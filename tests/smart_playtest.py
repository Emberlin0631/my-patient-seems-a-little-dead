"""像真人的試玩機器人：走商店流程、會買/用道具、喝水、補紀錄、休息，事件依「病人安全」挑選項。
用法：python3 tests/smart_playtest.py [局數] [班別 day|evening|night] [策略 expert|average|rookie]
  expert  ：每題選病人安全最高的選項（懂臨床的護理師）
  average ：75% 選最佳，25% 隨機（一般玩家）
  rookie  ：45% 選最佳（新手）
輸出：每局結局、數值曲線、事件統計 → tests/result_smart_<班>_<策略>.json
"""
import asyncio, json, sys, os, collections
from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = "file://" + os.path.abspath(os.environ.get("GAME_FILE") or os.path.join(HERE, "..", "index.html"))

BOT_JS = r"""
(policy) => {
  const R = { policy, ended:false, endFail:null, endReason:null, stuck:null, spawns:[], picks:[], expired:[], deaths:[], snaps:[],
              acts:{water:0, record:0, rest:0, items:{}}, walkMin:0, idleMin:0, busyMin:0, maxPending:0 };
  window.__R = R;
  const W = (name, fn) => { const o = window[name]; window[name] = function(){ try{ fn.apply(this, arguments); }catch(e){} return o.apply(this, arguments); }; };
  W('ringRoomAlarm', ev => { const d=ev.data||{}; R.spawns.push({t:gameState.currentTime, id:d.id, cat:d.category||'?', room:ev.patient&&ev.patient.assignedRoom}); });
  W('handleEventExpiry', ev => { const d=ev.data||{}; R.expired.push({t:gameState.currentTime, id:d.id, cat:d.category}); });
  W('killPatient', (p, why) => { R.deaths.push({t:gameState.currentTime, room:p.assignedRoom, why:(why||'').slice(0,40)}); });
  const origEnd = window.triggerEndShift;
  window.triggerEndShift = function(fail, reason){
    if(!R.ended){ R.ended=true; R.endFail=!!fail; R.endReason=(reason||'').slice(0,50);
      R.final={ho:gameState._handover, t:gameState.currentTime, stress:gameState.stress, stamina:gameState.stamina, hydration:gameState.hydration, records:gameState.records, safety:gameState.patientSafety, pending:gameState.pendingEvents.length, anx:gameState._anxTotal||0}; }
    return origEnd.apply(this, arguments);
  };
  setGameSpeed(5);
  const vis = id => { const e=document.getElementById(id); return e && getComputedStyle(e).display!=='none'; };
  const rank = c => ({emergent:1,ward:2,special:2,routine:3}[c]||3);
  const pBest = policy==='expert'?1:(policy==='average'?0.75:0.45);
  let lastT=-1, lastChange=Date.now(), pausedSince=null, lastSnap=-1;
  const have = id => { const b=backpack.find(i=>i.id===id); return b && ((b.portion? b.halves : b.count) > 0); };
  const use = (id, f) => { useBackpackItem(id, f); R.acts.items[id]=(R.acts.items[id]||0)+1; const m=document.getElementById('item-modal'); if(m) closeItemModal(); };
  setInterval(()=>{
    if(R.ended || gameState.isGameOver){ R.ended=true; return; }
    if(!gameState.speedLocked && gameState.gameSpeed!==5) setGameSpeed(5);
    const now=Date.now(), t=gameState.currentTime;
    if(t!==lastT){
      if(lastT>=0){ const d=t-lastT; if(gameState.isMoving) R.walkMin+=d; else if(gameState.pendingEvents.length) R.busyMin+=d; else R.idleMin+=d; }
      lastT=t; lastChange=now;
      R.maxPending=Math.max(R.maxPending, gameState.pendingEvents.length);
      if(t-lastSnap>=10){ lastSnap=t; const pc={}; gameState.pendingEvents.forEach(e=>{const c=e.data.category||'?'; pc[c]=(pc[c]||0)+1;});
        R.snaps.push([t, gameState.stress, gameState.stamina, gameState.hydration, gameState.records, gameState.patientSafety, pc]); }
    }
    const overlay = vis('event-overlay')||vis('multi-event-overlay')||vis('report-overlay')||document.querySelector('.info-modal.active');
    if(gameState.isPaused && !overlay){ if(!pausedSince) pausedSince=now; else if(now-pausedSince>20000 && !R.stuck) R.stuck='paused-without-overlay @'+t; } else pausedSince=null;
    if(now-lastChange>90000 && !R.stuck) R.stuck='clock-frozen @'+t;
    const am=document.querySelector('.info-modal.active');
    if(am && am.id==='brief-modal'){ closeShiftBrief(); return; }
    if(am && am.id==='handover-modal'){
      (gameState._handoverBeds||[]).forEach((bd,i)=>bd.items.forEach((it,j)=>{ const c=document.getElementById(`ho-${i}-${j}`); if(c) c.checked = Math.random()<pBest ? it.ok : Math.random()<0.5; }));
      R.handover=true; submitHandover(); return;
    }
    if(vis('event-overlay')){
      const ev=gameState.activeEvent, d=ev&&ev.data, opts=(d&&d.options)||[];
      const all=[...document.querySelectorAll('#options-container .option-btn')], shown=gameState._shownOptions||[];
      const btns=all.filter(b=>!b.disabled);
      if(!btns.length){ if(!R.stuck) R.stuck='event-with-no-enabled-option '+(d&&d.id); return; }
      const scored = btns.map(b=>{ const o=shown[all.indexOf(b)]||null; const txt=(o&&o.text)||b.innerText;
        const defer=!!(o&&o.defer) || /先去其他床|先放著|保留/.test(txt);
        let s = o ? (o.safety||0) : 0; if(o && (o.endShift||o.isHyperA||(o.endShiftP))) s=-999; if(/指責家屬/.test(txt)) s=-999; if(defer) s=-50;
        return {b, s, time:o?(o.time||0):0, txt:txt.slice(0,18), tag:o&&o.keyTag}; });
      scored.sort((a,b)=>b.s-a.s || a.time-b.time);
      const nonDefer = scored.filter(x=>x.s>-50);
      const pick = (Math.random()<pBest || !nonDefer.length) ? scored[0] : nonDefer[Math.floor(Math.random()*nonDefer.length)];
      R.picks.push({t, id:d&&d.id, cat:d&&d.category, best:pick===scored[0], s:pick.s, wait: t-((d&&(d.alarmSpawnTime||d.spawnTime))||t)});
      pick.b.click(); return;
    }
    if(vis('multi-event-overlay')){
      const bs=[...document.querySelectorAll('#multi-event-list-container button')];
      if(bs.length){ bs[0].click(); } else { const c=document.querySelector('#multi-event-overlay .event-header button'); c&&c.click(); }
      return;
    }
    if(overlay) return;
    if(gameState.isPaused || gameState.isMoving) return;
    const evs=gameState.pendingEvents.slice();
    const urgent=evs.filter(e=>['emergent','special','ward'].includes(e.data.category));
    // 道具
    if(gameState.stamina<40 && have('boba')) return use('boba');
    if(gameState.stamina<30 && !urgent.length && have('bento')) return use('bento',1);
    if(gameState.hydration<30 && have('iced_americano')) return use('iced_americano',1);
    if(gameState.stress>60 && have('snow_full')) return use('snow_full',1);
    // 護理站行動
    const atSt=isAtStation();
    if(gameState.hydration<35 && !evs.some(e=>e.data.category==='emergent')){ R.acts.water++; drinkWaterAction(); return; }
    if(atSt && gameState.hydration<75 && !urgent.length){ R.acts.water++; drinkWaterAction(); return; }
    if(atSt && gameState.records>=1 && !urgent.length && evs.length<=2){ R.acts.record++; writeRecordAction(); return; }
    if(gameState.records>=4 && !urgent.length){ R.acts.record++; writeRecordAction(); return; }
    if(atSt && gameState.stamina<60 && !evs.length){ R.acts.rest++; restAction(); return; }
    if(!evs.length){ if(gameState.records>0){ R.acts.record++; writeRecordAction(); } else if(!atSt) walkToStation(); return; }
    const lim=e=>{ const d=e.data; const st=d.alarmSpawnTime||d.spawnTime||e.time||t; return (d.deadlineMinutes||(d.category==='emergent'?25:999))-(t-st); };
    evs.sort((a,b)=>rank(a.data.category)-rank(b.data.category) || lim(a)-lim(b));
    if(evs[0].patient.isWard || evs[0].data.atStation){ if(isAtStation()) checkStationWard(); else walkToStation(); return; }
    onRoomCardClick(evs[0].patient.assignedRoom);
  }, 150);
}
"""

BUY = {
  'day':     {'boba': 2, 'bento': 1, 'iced_americano': 1, 'snow_full': 1},
  'evening': {'boba': 2, 'bento': 1, 'iced_americano': 2, 'snow_full': 1},
  'night':   {'boba': 2, 'bento': 1, 'iced_americano': 2, 'snow_full': 2},
  'intern':  {'boba': 2, 'bento': 1, 'iced_americano': 1, 'snow_full': 1},
}

async def run_one(browser, policy, shift, max_virtual_ms=3_600_000):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()
    errs = []
    page.on("pageerror", lambda e: errs.append(str(e)[:200]))
    await page.add_init_script("try{localStorage.setItem('gfxMode2','off')}catch(e){}")
    await page.clock.install(time=0)
    await page.goto(GAME)
    await page.evaluate(f"setShiftChoice('{shift}'); openShopScreen()")
    for k, n in BUY[shift].items():
        for _ in range(n):
            await page.evaluate(f"shopChange('{k}', 1)")
    await page.evaluate("confirmShopAndStart()")
    await page.evaluate(BOT_JS, policy)
    virtual = 0
    while virtual < max_virtual_ms:
        await page.clock.run_for(30_000); virtual += 30_000
        if os.environ.get('DBG'): print('  v', virtual, await page.evaluate('gameState.currentTime'), flush=True)
        if await page.evaluate("window.__R.ended || !!window.__R.stuck"):
            break
    R = await page.evaluate("window.__R")
    R['errors'] = errs[:5]
    R['game_time'] = await page.evaluate("gameState.currentTime")
    R['shiftEnd'] = await page.evaluate("gameState.shiftEndTime")
    R['report'] = await page.evaluate("(()=>{const e=document.getElementById('report-overlay'); return e && getComputedStyle(e).display!=='none' ? e.innerText.slice(0,600) : ''})()")
    await ctx.close()
    return R

async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    shift = sys.argv[2] if len(sys.argv) > 2 else 'day'
    policy = sys.argv[3] if len(sys.argv) > 3 else 'expert'
    out = []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for i in range(n):
            R = await run_one(b, policy, shift)
            out.append(R)
            f = R.get('final') or {}
            ot = (f.get('t', R['game_time']) - R['shiftEnd'])
            tag = ('通關' if not R['endFail'] else '失敗') if R['ended'] else '未結束'
            cats = collections.Counter(s['cat'] for s in R['spawns'])
            print(f"{shift} {policy} #{i} {tag} {R['endReason'][:34] if R['endReason'] else ''} | 加班{ot}分 | 壓{f.get('stress')} 體{f.get('stamina')} 水{f.get('hydration')} 紀{f.get('records')} 安{f.get('safety')} | 事件{dict(cats)} 逾時{len(R['expired'])} 死亡{len(R['deaths'])} | 走{R['walkMin']} 忙{R['busyMin']} 閒{R['idleMin']} | 水{R['acts']['water']} 紀{R['acts']['record']} 歇{R['acts']['rest']} 道具{R['acts']['items']} 交班{f.get('ho')} {'STUCK:'+R['stuck'] if R['stuck'] else ''} {R['errors'] or ''}", flush=True)
        await b.close()
    outdir = os.environ.get('OUT_DIR') or HERE
    json.dump(out, open(os.path.join(outdir, f"{os.environ.get('RUN_TAG','result_smart')}_{shift}_{policy}.json"), 'w'), ensure_ascii=False)

if __name__ == "__main__":
    asyncio.run(main())
