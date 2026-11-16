(function(){
  if(window.__dcWidgetLoaded)return;
  window.__dcWidgetLoaded=true;

  function dcEsc(s){
    if(s===null||s===undefined)return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  function dcNormTeamNames(obj){if(!obj)return obj;var lg=String(obj.league||obj.league_code||'').toUpperCase(),sp=String(obj.sport||'').toLowerCase(),h=String(obj.home||'').trim(),a=String(obj.away||'').trim();if(lg==='F1'||sp==='f1'){if(!h||h==='Home')obj.home='베르스타펜 (레드불)';if(!a||a==='Away')obj.away='노리스 (맥라렌)';if(obj.status_badge&&String(obj.status_badge).indexOf('EST')!==-1)obj.status_badge='퀄리파잉 · 결승 세션';if(obj.status_text&&String(obj.status_text).indexOf('EST')!==-1)obj.status_text='퀄리파잉 · 결승 세션';}else if(lg==='WTA'){if(!h||h==='Home')obj.home='사발렌카';if(!a||a==='Away')obj.away='시비옹테크';}else if(lg==='ATP'||sp==='tennis'){if(!h||h==='Home')obj.home='신네르';if(!a||a==='Away')obj.away='알카라스';}else{if((!h||h==='Home')&&(!a||a==='Away')){obj.home='홈';obj.away='원정';}}if(obj.match&&String(obj.match).indexOf('Home vs Away')!==-1){var rep=(lg==='F1'||sp==='f1')?'베르스타펜 (레드불) vs 노리스 (맥라렌)':(lg==='WTA'?'사발렌카 vs 시비옹테크':((lg==='ATP'||sp==='tennis')?'신네르 vs 알카라스':'홈 vs 원정'));obj.match=String(obj.match).replace(/Home vs Away/g,rep);}return obj;} function dcNum(n,d){
    var v=Number(n);
    return isNaN(v)?'-':(d!==undefined?v.toFixed(d):String(v));
  }
  function dcGetJson(url,cb){
    fetch(url,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){cb(null,d);}).catch(function(e){cb(e,null);});
  }
  function dcPostJson(url,cb){
    fetch(url,{method:'POST',credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){cb(null,d);}).catch(function(e){cb(e,null);});
  }

  var css = [
    '#dcTickerControls{display:flex;flex-wrap:wrap;align-items:center;gap:6px;width:100%;padding:6px 0;border-top:1px solid rgba(51,65,85,.7);margin-top:4px}',
    '.dc-pill{background:#0f172a;color:#cbd5e1;border:1px solid #334155;border-radius:999px;padding:4px 10px;font-size:12px;cursor:pointer;display:inline-flex;align-items:center;gap:4px;white-space:nowrap;transition:all .15s}',
    '.dc-pill:hover{border-color:#38bdf8;color:#fff}',
    '.dc-pill.active{background:#0284c7;border-color:#38bdf8;color:#fff;font-weight:700}',
    '.dc-select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:4px 8px;font-size:12px}',
    '#dcLiveTickerTrack{display:flex;gap:10px;overflow-x:auto;padding:8px 2px;width:100%;scrollbar-width:thin}',
    '.dc-mcard{min-width:255px;max-width:270px;flex:0 0 auto;background:#0f172a;border:1px solid #1e293b;border-radius:12px;padding:10px 12px;cursor:pointer;transition:border-color .15s,transform .15s}',
    '.dc-mcard:hover{border-color:#38bdf8;transform:translateY(-1px)}',
    '.dc-mcard-top{display:flex;justify-content:space-between;align-items:center;gap:4px;font-size:11px;color:#94a3b8;margin-bottom:6px}',
    '.dc-lbadge{background:#1e293b;color:#38bdf8;padding:2px 6px;border-radius:6px;font-weight:700;font-size:10px}',
    '.dc-sbadge{padding:2px 6px;border-radius:6px;font-weight:700;font-size:10px}',
    '.dc-sbadge.in{background:rgba(239,68,68,.2);color:#f87171;border:1px solid rgba(239,68,68,.4)}',
    '.dc-sbadge.pre{background:rgba(56,189,248,.15);color:#38bdf8}',
    '.dc-sbadge.post{background:rgba(148,163,184,.15);color:#94a3b8}',
    '.dc-trow{display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:600;color:#f1f5f9;margin:2px 0}',
    '.dc-score{font-size:15px;font-weight:800;color:#38bdf8}',
    '.dc-periods{font-size:11px;color:#94a3b8;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.dc-airow{font-size:11px;color:#a5f3fc;background:rgba(14,116,144,.2);border-radius:6px;padding:4px 6px;margin-top:6px}',
    '#dcMainHubSection{max-width:80rem;margin:14px auto;padding:0 16px}',
    '.dc-hub-wrap{background:#0b1220;border:1px solid #1e293b;border-radius:16px;padding:16px;color:#e2e8f0;box-shadow:0 10px 30px rgba(0,0,0,.35)}',
    '.dc-hub-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:10px;padding-bottom:12px;border-bottom:1px solid #1e293b}',
    '.dc-live-dot{display:inline-flex;align-items:center;gap:6px;background:rgba(16,185,129,.15);color:#34d399;border:1px solid rgba(16,185,129,.4);padding:3px 9px;border-radius:999px;font-size:11px;font-weight:700}',
    '.dc-btn{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}',
    '.dc-btn:hover{border-color:#38bdf8;color:#fff}',
    '.dc-btn-primary{background:#0284c7;border-color:#38bdf8;color:#fff}',
    '.dc-launcher-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:14px 0}',
    '.dc-lcard{background:#111a2e;border:1px solid #1e293b;border-radius:12px;padding:12px;cursor:pointer;text-align:left;transition:all .15s}',
    '.dc-lcard:hover{border-color:#38bdf8}',
    '.dc-lcard.active{background:linear-gradient(135deg,#0c2444,#0f172a);border-color:#38bdf8;box-shadow:0 0 0 1px #38bdf8}',
    '.dc-lcard-title{font-size:13px;font-weight:700;color:#f8fafc}',
    '.dc-lcard-sub{font-size:11px;color:#94a3b8;margin-top:3px}',
    '.dc-grid-2{display:grid;grid-template-columns:1.35fr 1fr;gap:14px;align-items:start}',
    '.dc-kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}',
    '.dc-kpi{background:#111a2e;border:1px solid #1e293b;border-radius:10px;padding:10px 12px}',
    '.dc-kpi-lbl{font-size:11px;color:#94a3b8}',
    '.dc-kpi-val{font-size:15px;font-weight:700;color:#f8fafc;margin-top:3px}',
    '.dc-card{background:#111a2e;border:1px solid #1e293b;border-radius:12px;padding:12px;overflow-x:auto}',
    '.dc-tbl{width:100%;border-collapse:collapse;font-size:12px}',
    '.dc-tbl th{text-align:left;color:#94a3b8;font-weight:600;padding:7px 6px;border-bottom:1px solid #1e293b;white-space:nowrap}',
    '.dc-tbl td{padding:7px 6px;border-bottom:1px solid rgba(30,41,59,.6);color:#e2e8f0;white-space:nowrap}',
    '.dc-zone-ucl{border-left:3px solid #10b981;padding-left:6px}',
    '.dc-zone-uel{border-left:3px solid #38bdf8;padding-left:6px}',
    '.dc-zone-po{border-left:3px solid #f59e0b;padding-left:6px}',
    '.dc-zone-rel{border-left:3px solid #ef4444;padding-left:6px}',
    '.dc-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px}',
    '.dc-dot-W{background:#22c55e}.dc-dot-D{background:#94a3b8}.dc-dot-L{background:#ef4444}',
    '.dc-bar-bg{width:64px;height:7px;background:#1e293b;border-radius:999px;display:inline-block;overflow:hidden;vertical-align:middle;margin-right:4px}',
    '.dc-bar-fg{height:100%;background:#38bdf8;display:block}',
    '.dc-hl-row{background:rgba(16,185,129,.12)}',
    '#dcToastWrap{position:fixed;top:16px;right:16px;z-index:99999;display:flex;flex-direction:column;gap:8px;max-width:340px}',
    '.dc-toast{background:#0f172a;color:#f8fafc;border:1px solid #38bdf8;border-radius:10px;padding:10px 14px;font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,.45)}',
    '#dcMatchModal{position:fixed;inset:0;background:rgba(2,6,23,.78);z-index:99998;display:none;align-items:center;justify-content:center;padding:16px}',
    '.dc-modal-box{background:#0f172a;border:1px solid #334155;border-radius:16px;max-width:500px;width:100%;padding:18px;color:#f1f5f9}',
    '@media(max-width:860px){.dc-grid-2{grid-template-columns:1fr}.dc-launcher-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}'
  ].join('\n');
  var st=document.createElement('style');
  st.textContent=css;
  document.head.appendChild(st);

  var toastWrap=document.createElement('div');
  toastWrap.id='dcToastWrap';
  document.body.appendChild(toastWrap);

  function showToast(msg){
    var el=document.createElement('div');
    el.className='dc-toast';
    el.textContent=msg;
    toastWrap.appendChild(el);
    setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el);},4500);}function showOddsToast(msg){var el=document.createElement('div');el.className='dc-toast';el.style.cssText='background:linear-gradient(135deg,rgba(124,58,237,0.96),rgba(217,119,6,0.95));color:#fff;border:1px solid rgba(251,191,36,0.6);box-shadow:0 8px 24px rgba(88,28,135,0.45);font-weight:700;';el.textContent=msg;toastWrap.appendChild(el);setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el);},5200);
  }
  function playBeep(){
    try{
      var Ctx=window.AudioContext||window.webkitAudioContext;
      if(!Ctx)return;
      var ctx=new Ctx();
      var osc=ctx.createOscillator();
      var gain=ctx.createGain();
      osc.type='sine';
      osc.frequency.value=880;
      gain.gain.value=0.06;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      setTimeout(function(){osc.stop();ctx.close();},180);
    }catch(e){}
  }

  var state={
    sport:'all',
    league:'all',oddsAlertOn:localStorage.getItem('dc_odds_alert')!=='0',seenOddsAlerts:{},counts:{},dropSport:'all',dropMin:5,
    dateOffset:0,
    alertOn:localStorage.getItem('sportiq_dc_score_alert')!=='0',
    prevScores:{},
    seenAlerts:{},
    matches:[],
    tab:'standings',
    collapsed:false,
    standingsLeague:'EPL',
    seasonLeague:'EPL',
    transferCat:'ALL',
    transferFilter:'TOP',
    injuryLeague:'EPL',
    injurySeverity:'ALL',
    injuryQuery:''
  };

  var SPORTS=[
    {id:'all',lbl:'🌐 전체',cntKey:'all'},
    {id:'soccer',lbl:'⚽ 축구',cntKey:'soccer'},
    {id:'baseball',lbl:'⚾ 야구',cntKey:'baseball'},
    {id:'basketball',lbl:'🏀 농구',cntKey:'basketball'},{id:'volleyball',lbl:'🏐 배구(V-리그)',cntKey:'volleyball'},
    {id:'hockey',lbl:'🏒 하키',cntKey:'hockey'},
    {id:'esports',lbl:'🎮 LCK',cntKey:'esports'},{id:'ufc',lbl:'🥊 UFC',cntKey:'ufc'},{id:'tennis',lbl:'🎾 테니스',cntKey:'tennis'},{id:'f1',lbl:'🏎️ F1',cntKey:'f1'}
  ];
  var LEAGUES18=['all','EPL','LALIGA','BUNDESLIGA','SERIEA','LIGUE1','KLEAGUE','JLEAGUE','MLS','UCL','UNL','WC2026','KBO','MLB','NPB','NBA','WNBA','VLEAGUE_M','VLEAGUE_W','NHL','LCK','UFC','ATP','WTA','F1'];
  var LEAGUES11=['EPL','LALIGA','BUNDESLIGA','SERIEA','LIGUE1','KLEAGUE','NBA','KBO','MLB','NHL','LCK'];

  var modal=document.createElement('div');
  modal.id='dcMatchModal';
  modal.innerHTML='<div class="dc-modal-box" id="dcMatchModalBody"></div>';
  modal.onclick=function(e){if(e.target===modal)modal.style.display='none';};
  document.body.appendChild(modal);

  function openMatchModal(m){
    var box=document.getElementById('dcMatchModalBody');
    var pstr=(m.period_scores&&m.period_scores.length)?m.period_scores.join(' / '):'세부 스코어 집계 중';
    var ai=m.ai||{};
    var odds=ai.odds||{};
    var lgCode=m.league_code||m.league||'EPL';
    var hasLeague=LEAGUES11.indexOf(lgCode)>=0?lgCode:'EPL';
    box.innerHTML=
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">'
      +'<span class="dc-lbadge">'+dcEsc(m.league||lgCode)+'</span>'
      +'<button class="dc-btn" id="dcCloseModalBtn">닫기 ✕</button>'
      +'</div>'
      +'<div style="font-size:12px;color:#94a3b8;margin-bottom:8px">📅 '+dcEsc(m.match_dt_full_kst||m.match_dt_kst)+' · 🏟️ '+dcEsc(m.venue||'구장 정보 없음')+'</div>'
      +'<div style="display:flex;justify-content:space-between;align-items:center;background:#1e293b;border-radius:12px;padding:14px;margin-bottom:10px">'
      +'<div style="font-size:15px;font-weight:700">'+dcEsc(m.home)+'</div>'
      +'<div style="font-size:22px;font-weight:800;color:#38bdf8">'+dcEsc(m.home_score)+' : '+dcEsc(m.away_score)+'</div>'
      +'<div style="font-size:15px;font-weight:700">'+dcEsc(m.away)+'</div>'
      +'</div>'
      +'<div style="font-size:12px;color:#cbd5e1;margin-bottom:8px">📊 세부 점수: '+dcEsc(pstr)+'</div>'
      +'<div class="dc-airow" style="margin-bottom:12px">🎯 AI 승무패 확률: 홈 '+dcEsc(ai.home_pct)+'% / 무 '+dcEsc(ai.draw_pct)+'% / 원정 '+dcEsc(ai.away_pct)+'% · 배당 ('+dcEsc(odds.home)+' / '+dcEsc(odds.draw)+' / '+dcEsc(odds.away)+')</div>'
      +'<div style="display:flex;gap:8px;flex-wrap:wrap">'
      +'<button class="dc-btn dc-btn-primary" id="dcGoStandingsBtn">🔢 '+dcEsc(hasLeague)+' 순위표 보기</button>'
      +'<button class="dc-btn" id="dcGoInjuriesBtn">🏥 '+dcEsc(hasLeague)+' 부상자 보기</button>'
      +'</div>';
    modal.style.display='flex';
    document.getElementById('dcCloseModalBtn').onclick=function(){modal.style.display='none';};
    document.getElementById('dcGoStandingsBtn').onclick=function(){
      modal.style.display='none';
      state.standingsLeague=hasLeague;
      switchHubTab('standings',true);
    };
    document.getElementById('dcGoInjuriesBtn').onclick=function(){
      modal.style.display='none';
      state.injuryLeague=hasLeague;
      switchHubTab('injuries',true);
    };
  }

  function setupTickerBar(){
    var bar=document.getElementById('proLiveTickerBar');
    if(!bar)return;
    var oldList=document.getElementById('proTickerList');
    if(oldList)oldList.style.display='none';
    var inner=bar.querySelector('.max-w-7xl')||bar;

    var ctrl=document.createElement('div');
    ctrl.id='dcTickerControls';
    inner.appendChild(ctrl);

    var track=document.createElement('div');
    track.id='dcLiveTickerTrack';
    track.innerHTML='<span style="font-size:12px;color:#94a3b8">실시간 스코어를 불러오는 중...</span>';
    inner.appendChild(track);

    window.loadProLiveTicker=loadLiveScores;
    renderTickerControls({});
    loadLiveScores();
    setInterval(loadLiveScores,25000);
  }

  function renderTickerControls(counts){
    var ctrl=document.getElementById('dcTickerControls');
    if(!ctrl)return;
    var html='';
    for(var i=0;i<SPORTS.length;i++){
      var sp=SPORTS[i];
      var cnt=counts&&counts[sp.cntKey]!==undefined?counts[sp.cntKey]:'';
      var act=state.sport===sp.id?' active':'';
      html+='<button class="dc-pill'+act+'" data-dcsport="'+dcEsc(sp.id)+'">'+dcEsc(sp.lbl)+(cnt!==''?' <span>('+dcEsc(cnt)+')</span>':'')+'</button>';
    }
    html+='<select class="dc-select" id="dcLeagueSel">';
    for(var j=0;j<LEAGUES18.length;j++){
      var lg=LEAGUES18[j];
      var lbl=lg==='all'?'전체 18개 리그':lg;
      html+='<option value="'+dcEsc(lg)+'"'+(state.league===lg?' selected':'')+'>'+dcEsc(lbl)+'</option>';
    }
    html+='</select>';
    var dates=[{v:-1,l:'◀ 어제'},{v:0,l:'오늘 · 실시간'},{v:1,l:'내일 ▶'},{v:2,l:'+2일'}];
    for(var k=0;k<dates.length;k++){
      var dt=dates[k];
      html+='<button class="dc-pill'+(state.dateOffset===dt.v?' active':'')+'" data-dcdate="'+dt.v+'">'+dcEsc(dt.l)+'</button>';
    }
    html+='<button class="dc-pill'+(state.alertOn?' active':'')+'" id="dcAlertToggleBtn">'+(state.alertOn?'🔔 득점알림 ON':'🔕 득점알림 OFF')+'</button>';
    html+='<button class="dc-pill'+(state.oddsAlertOn?' active':'')+'" id="dcOddsAlertToggleBtn">'+(state.oddsAlertOn?'📉 배당급변 알림 ON':'🔕 배당알림 OFF')+'</button>';ctrl.innerHTML=html;

    var sBtns=ctrl.querySelectorAll('[data-dcsport]');
    for(var a=0;a<sBtns.length;a++){
      sBtns[a].onclick=function(){
        state.sport=this.getAttribute('data-dcsport');
        loadLiveScores();
      };
    }
    document.getElementById('dcLeagueSel').onchange=function(){
      state.league=this.value;
      loadLiveScores();
    };
    var dBtns=ctrl.querySelectorAll('[data-dcdate]');
    for(var b=0;b<dBtns.length;b++){
      dBtns[b].onclick=function(){
        state.dateOffset=Number(this.getAttribute('data-dcdate'));
        loadLiveScores();
       var ob=document.getElementById('dcOddsAlertToggleBtn');if(ob){ob.onclick=function(){state.oddsAlertOn=!state.oddsAlertOn;localStorage.setItem('dc_odds_alert',state.oddsAlertOn?'1':'0');renderTickerControls(counts);if(state.oddsAlertOn){showOddsToast('📉 드롭 오즈 실시간 경보가 켜졌습니다.');playBeep();}else{showToast('🔕 배당급변 알림이 꺼졌습니다.');}};};
      };
    }
    document.getElementById('dcAlertToggleBtn').onclick=function(){
      state.alertOn=!state.alertOn;
      localStorage.setItem('sportiq_dc_score_alert',state.alertOn?'1':'0');
      renderTickerControls(counts);
      showToast(state.alertOn?'🔔 실시간 득점 알림이 켜졌습니다.':'🔕 실시간 득점 알림이 꺼졌습니다.');
    };
  }

  function loadLiveScores(){
    var url='/api/datacenter/live-scores?sport='+encodeURIComponent(state.sport)+'&league='+encodeURIComponent(state.league)+'&date_offset='+encodeURIComponent(state.dateOffset);
    dcGetJson(url,function(err,data){
      if(err||!data||!data.ok)return;
      renderTickerControls(data.counts||{});
      var matches=(data.matches||[]).map(dcNormTeamNames);
      state.matches=matches;
      var updated=document.getElementById('proTickerUpdatedAt');
      if(updated&&data.server_now_kst)updated.textContent='KST '+data.server_now_kst;

      var alerted=false;
      for(var i=0;i<matches.length;i++){
        var m=matches[i];
        var key=String(m.id||i);
        var cur=String(m.home_score)+'-'+String(m.away_score);
        if(state.prevScores[key]&&state.prevScores[key]!==cur){
          if(state.alertOn){
            showToast('⚽ 득점 알림 ['+m.league+'] '+m.home+' '+m.home_score+' : '+m.away_score+' '+m.away);
            alerted=true;
          }
        }
        state.prevScores[key]=cur;
      }
      var sal=data.score_alerts||[];
      for(var j=0;j<sal.length;j++){
        var al=sal[j];
        if(al.id&&!state.seenAlerts[al.id]){
          state.seenAlerts[al.id]=true;
          if(state.alertOn&&Object.keys(state.seenAlerts).length>1){
            showToast('🔔 '+al.text);
            alerted=true;
          }
        }
      }
      var oal=data.odds_alerts||[];for(var oj=0;oj<oal.length;oj++){var oa=oal[oj];if(oa.id&&!state.seenOddsAlerts[oa.id]){state.seenOddsAlerts[oa.id]=true;if(state.oddsAlertOn&&Object.keys(state.seenOddsAlerts).length>1){showOddsToast('📉 ['+(oa.league||oa.sport||'')+'] '+oa.text);alerted=true;}}}if(alerted)playBeep();

      var track=document.getElementById('dcLiveTickerTrack');
      if(!track)return;
      if(!matches.length){
        track.innerHTML='<div style="font-size:12px;color:#94a3b8;padding:10px">선택한 조건에 해당하는 경기가 없습니다.</div>';
        return;
      }
      var html='';
      for(var k=0;k<matches.length;k++){
        var x=matches[k];
        var stCls=x.state==='in'?'in':(x.state==='post'?'post':'pre');
        var stTxt=x.status_badge||(x.state==='in'?'LIVE':(x.state==='post'?'종료':'예정'));
        var pstr=(x.period_scores&&x.period_scores.length)?x.period_scores.join(' · '):'세부 스코어 대기';
        var ai=x.ai||{};
        var odds=ai.odds||{};var oddsTxt=Number(odds.draw)>0?(dcEsc(odds.home)+'/'+dcEsc(odds.draw)+'/'+dcEsc(odds.away)):(dcEsc(odds.home)+' / '+dcEsc(odds.away));var dropB=x.drop_badge?'<span style="display:inline-block;margin-left:4px;padding:1px 6px;border-radius:999px;font-size:10px;font-weight:800;background:linear-gradient(135deg,rgba(245,158,11,0.22),rgba(239,68,68,0.25));color:#fbbf24;border:1px solid rgba(245,158,11,0.55);">'+dcEsc(x.drop_badge)+'</span>':'';
        html+='<div class="dc-mcard" data-midx="'+k+'">'
          +'<div class="dc-mcard-top"><span class="dc-lbadge">'+dcEsc(x.league_code||x.league)+'</span>'+dropB+'<span>📅 '+dcEsc(x.match_dt_kst)+'</span><span class="dc-sbadge '+stCls+'">'+dcEsc(stTxt)+'</span></div>'
          +'<div class="dc-trow"><span>'+dcEsc(x.home)+'</span><span class="dc-score">'+dcEsc(x.home_score)+'</span></div>'
          +'<div class="dc-trow"><span>'+dcEsc(x.away)+'</span><span class="dc-score">'+dcEsc(x.away_score)+'</span></div>'
          +'<div class="dc-periods">📊 '+dcEsc(pstr)+'</div>'
          +'<div class="dc-airow">🎯 AI '+dcEsc(ai.home_pct)+'% / '+dcEsc(ai.draw_pct)+'% / '+dcEsc(ai.away_pct)+'% · 배당 '+oddsTxt+'</div>'
          +'</div>';
      }
      track.innerHTML=html;
      var cards=track.querySelectorAll('.dc-mcard');
      for(var c=0;c<cards.length;c++){
        cards[c].onclick=function(){
          var idx=Number(this.getAttribute('data-midx'));
          if(state.matches[idx])openMatchModal(state.matches[idx]);
        };
      }
    });
  }

  function setupMainHub(){
    var bar=document.getElementById('proLiveTickerBar');
    if(!bar)return;
    var sec=document.createElement('section');
    sec.id='dcMainHubSection';
    sec.innerHTML=
      '<div class="dc-hub-wrap">'
      +'<div class="dc-hub-head">'
      +'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">'
      +'<span class="dc-live-dot">● AUTO LIVE</span>'
      +'<h2 style="font-size:17px;font-weight:800;margin:0;color:#f8fafc">📊 SportIQ 데이터 센터 · 9종목 · 24리그 · 드롭오즈 실시간 연동</h2>'
      +'</div>'
      +'<div style="display:flex;gap:8px;align-items:center">'
      +'<button class="dc-btn dc-btn-primary" id="dcSyncNowBtn">🔄 즉시 자동수집 동기화</button>'
      +'<button class="dc-btn" id="dcCollapseBtn">접기 ▲</button>'
      +'</div>'
      +'</div>'
      +'<div id="dcHubCollapsible">'
      +'<div class="dc-launcher-grid" id="dcLauncherGrid"></div>'
      +'<div id="dcHubBody"><div style="padding:18px;text-align:center;color:#94a3b8;font-size:13px">데이터를 불러오는 중...</div></div>'
      +'</div>'
      +'</div>';
    bar.insertAdjacentElement('afterend',sec);

    var nav=document.querySelector('header nav, nav');
    if(nav){
      var nb=document.createElement('button');
      nb.type='button';
      nb.className='dc-pill';
      nb.textContent='📊 데이터센터';
      nb.onclick=function(){
        state.collapsed=false;
        applyCollapse();
        sec.scrollIntoView({behavior:'smooth'});
      };
      nav.appendChild(nb);
    }

    document.getElementById('dcSyncNowBtn').onclick=function(){
      var btn=this;
      btn.disabled=true;
      btn.textContent='⏳ 동기화 중...';
      dcPostJson('/api/datacenter/sync-now',function(err,res){
        btn.disabled=false;
        btn.textContent='🔄 즉시 자동수집 동기화';
        if(!err&&res&&res.ok){
          showToast('✅ 자동수집 동기화 완료 ('+dcEsc(res.synced_at_kst)+')');
          loadLiveScores();
          loadHubTab();
        }else{
          showToast('⚠️ 동기화 요청에 실패했습니다.');
        }
      });
    };
    document.getElementById('dcCollapseBtn').onclick=function(){
      state.collapsed=!state.collapsed;
      applyCollapse();
    };

    renderLaunchers();
    loadHubTab();
  }

  function applyCollapse(){
    var col=document.getElementById('dcHubCollapsible');
    var btn=document.getElementById('dcCollapseBtn');
    if(col)col.style.display=state.collapsed?'none':'block';
    if(btn)btn.textContent=state.collapsed?'펼치기 ▼':'접기 ▲';
  }

  function switchHubTab(tabId,scroll){
    state.tab=tabId;
    state.collapsed=false;
    applyCollapse();
    renderLaunchers();
    loadHubTab();
    if(scroll){
      var sec=document.getElementById('dcMainHubSection');
      if(sec)sec.scrollIntoView({behavior:'smooth'});
    }
  }

  function renderLaunchers(){
    var grid=document.getElementById('dcLauncherGrid');
    if(!grid)return;
    var items=[
      {id:'standings',t:'🔢 리그 순위 (실시간)',s:'11개 리그 순위·득점/도움왕'},
      {id:'season',t:'📊 AI 시즌 예측',s:'Elo·5,000회 시뮬·FIFA/클럽'},
      {id:'transfers',t:'💶 해외 이적시장',s:'AI 브리핑·유럽/코리안 몸값'},
      {id:'injuries',t:'🏥 부상자 명단',s:'구단별 결장자·전력 타격'},
      {id:'scorecard',t:'🎯 적중률 투명 공개',s:'4대 AI·회원 승부예측 성적표'},{id:'dropping-odds',t:'📉 배당 급변 · 드롭 오즈',s:'9종목 실시간 스팀·확률 급변'}
    ];
    var html='';
    for(var i=0;i<items.length;i++){
      var it=items[i];
      html+='<button class="dc-lcard'+(state.tab===it.id?' active':'')+'" data-dctab="'+it.id+'" data-tab="'+it.id+'">'
        +'<div class="dc-lcard-title">'+dcEsc(it.t)+'</div>'
        +'<div class="dc-lcard-sub">'+dcEsc(it.s)+'</div>'
        +'</button>';
    }
    grid.innerHTML=html;
    var btns=grid.querySelectorAll('[data-dctab]');
    for(var j=0;j<btns.length;j++){
      btns[j].onclick=function(){switchHubTab(this.getAttribute('data-dctab'),false);};
    }
  }

  function leaguePillsHtml(activeLg,attrName){
    var h='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">';
    for(var i=0;i<LEAGUES11.length;i++){
      var lg=LEAGUES11[i];
      h+='<button class="dc-pill'+(activeLg===lg?' active':'')+'" '+attrName+'="'+dcEsc(lg)+'">'+dcEsc(lg)+'</button>';
    }
    return h+'</div>';
  }

  function loadHubTab(){
    var body=document.getElementById('dcHubBody');
    if(!body)return;
    body.innerHTML='<div style="padding:18px;text-align:center;color:#94a3b8;font-size:13px">데이터를 불러오는 중...</div>';
    if(state.tab==='standings')renderStandings(body);
    else if(state.tab==='season')renderSeason(body);
    else if(state.tab==='transfers')renderTransfers(body);
    else if(state.tab==='injuries')renderInjuries(body);
    else if(state.tab==='scorecard')renderScorecard(body);else if(state.tab==='dropping-odds')renderDroppingOdds(body);;
  }

  function renderStandings(body){
    dcGetJson('/api/datacenter/standings?league='+encodeURIComponent(state.standingsLeague),function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">순위 데이터를 불러오지 못했습니다.</div>';return;}
      var rows=d.standings||[];
      var leaders=d.leaders||{};
      var sc=leaders.scorers||[];
      var as=leaders.assists||[];
      var h=leaguePillsHtml(state.standingsLeague,'data-stlg');h+='<div style="display:flex;flex-wrap:wrap;gap:6px;margin:-4px 0 12px">';var ext=[{id:'VLEAGUE',l:'🏐 V-리그'},{id:'UFC',l:'🥊 UFC 랭킹'},{id:'TENNIS',l:'🎾 ATP 랭킹'},{id:'F1',l:'🏎️ F1 순위'}];for(var ei=0;ei<ext.length;ei++){var ex=ext[ei];h+='<button class="dc-pill'+(state.standingsLeague===ex.id?' active':'')+'" data-stlg="'+ex.id+'">'+ex.l+'</button>';}h+='</div>';
      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🏆 '+dcEsc(d.league_name||d.league)+' 실시간 순위 ('+dcEsc(d.updated_at_kst)+')</div>';
      h+='<table class="dc-tbl"><thead><tr><th>#</th><th>구단</th><th>경기</th><th>승</th><th>무</th><th>패</th><th>득실</th><th>최근5</th><th>승점/승률</th></tr></thead><tbody>';
      for(var i=0;i<rows.length;i++){
        var r=rows[i];
        var zc=r.zone==='UCL'?'dc-zone-ucl':(r.zone==='UEL'?'dc-zone-uel':(r.zone==='PO'?'dc-zone-po':(r.zone==='REL'?'dc-zone-rel':'')));
        var fm='';
        var arr=r.form||[];
        for(var f=0;f<arr.length;f++){fm+='<span class="dc-dot dc-dot-'+dcEsc(arr[f])+'"></span>';}
        h+='<tr><td><span class="'+zc+'">'+dcEsc(r.rank)+'</span></td><td style="font-weight:600">'+dcEsc(r.team)+'</td><td>'+dcEsc(r.gp)+'</td><td>'+dcEsc(r.w)+'</td><td>'+dcEsc(r.d)+'</td><td>'+dcEsc(r.l)+'</td><td>'+dcEsc(r.gd)+'</td><td>'+fm+'</td><td style="font-weight:700;color:#38bdf8">'+dcEsc(r.pts_label||r._pts||r.pts)+'</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div style="display:flex;flex-direction:column;gap:12px">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">👑 득점·핵심 타이틀 리더보드</div><table class="dc-tbl"><thead><tr><th>#</th><th>선수</th><th>구단</th><th>기록</th></tr></thead><tbody>';
      for(var s=0;s<sc.length;s++){
        var sr=sc[s];
        h+='<tr><td>'+dcEsc(sr.rank)+'</td><td style="font-weight:600">'+dcEsc(sr.player)+'</td><td>'+dcEsc(sr.team)+'</td><td style="color:#fbbf24;font-weight:700">'+dcEsc(sr.stat)+' '+dcEsc(sr.sub||'')+'</td></tr>';
      }
      h+='</tbody></table></div>';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🎯 도움·보조 타이틀 리더보드</div><table class="dc-tbl"><thead><tr><th>#</th><th>선수</th><th>구단</th><th>기록</th></tr></thead><tbody>';
      for(var a=0;a<as.length;a++){
        var ar=as[a];
        h+='<tr><td>'+dcEsc(ar.rank)+'</td><td style="font-weight:600">'+dcEsc(ar.player)+'</td><td>'+dcEsc(ar.team)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(ar.stat)+' '+dcEsc(ar.sub||'')+'</td></tr>';
      }
      h+='</tbody></table></div></div></div>';
      body.innerHTML=h;
      var btns=body.querySelectorAll('[data-stlg]');
      for(var b=0;b<btns.length;b++){
        btns[b].onclick=function(){state.standingsLeague=this.getAttribute('data-stlg');loadHubTab();};
      }
    });
  }

  function renderSeason(body){
    dcGetJson('/api/datacenter/season-sim?league='+encodeURIComponent(state.seasonLeague),function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">시즌 예측 데이터를 불러오지 못했습니다.</div>';return;}
      var tbl=d.table||[];
      var fifa=d.fifa_ranking||[];
      var club=d.club_ranking||[];
      var h=leaguePillsHtml(state.seasonLeague,'data-sslg');
      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">📊 '+dcEsc(d.league_name||d.league)+' Elo & Monte Carlo '+dcEsc(d.simulations||5000)+'회 예측</div>';
      h+='<table class="dc-tbl"><thead><tr><th>예상</th><th>구단</th><th>Elo</th><th>현재→예상</th><th>🏆 우승%</th><th>🌟 '+dcEsc(d.po_label||'UCL/PO')+'%</th><th>⚠️ 강등%</th></tr></thead><tbody>';
      for(var i=0;i<tbl.length;i++){
        var r=tbl[i];
        var w=Math.min(100,Math.max(0,Number(r.title_prob)||0));
        h+='<tr><td>'+dcEsc(r.sim_rank)+'</td><td style="font-weight:600">'+dcEsc(r.team)+'</td><td>'+dcEsc(r.elo)+'</td><td>'+dcEsc(r.cur_pts)+'→<b>'+dcEsc(r.proj_pts)+'</b></td><td><span class="dc-bar-bg"><span class="dc-bar-fg" style="width:'+w+'%"></span></span>'+dcEsc(r.title_prob)+'%</td><td style="color:#34d399">'+dcEsc(r.po_prob)+'%</td><td style="color:#f87171">'+dcEsc(r.rel_prob)+'%</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div style="display:flex;flex-direction:column;gap:12px">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🌍 FIFA 국가대표 랭킹</div><table class="dc-tbl"><thead><tr><th>#</th><th>국가</th><th>포인트</th><th>등락</th></tr></thead><tbody>';
      for(var f=0;f<fifa.length;f++){
        var fr=fifa[f];
        h+='<tr class="'+(fr.highlight?'dc-hl-row':'')+'"><td>'+dcEsc(fr.rank)+'</td><td style="font-weight:600">'+dcEsc(fr.flag||'')+' '+dcEsc(fr.country)+'</td><td>'+dcEsc(fr.points)+'</td><td>'+dcEsc(fr.change)+'</td></tr>';
      }
      h+='</tbody></table></div>';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🏟️ 세계 클럽 Elo 파워 랭킹</div><table class="dc-tbl"><thead><tr><th>#</th><th>클럽</th><th>리그</th><th>Elo</th><th>등락</th></tr></thead><tbody>';
      for(var c=0;c<club.length;c++){
        var cr=club[c];
        h+='<tr><td>'+dcEsc(cr.rank)+'</td><td style="font-weight:600">'+dcEsc(cr.club)+'</td><td>'+dcEsc(cr.league)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(cr.elo)+'</td><td>'+dcEsc(cr.change)+'</td></tr>';
      }
      h+='</tbody></table></div></div></div>';
      body.innerHTML=h;
      var btns=body.querySelectorAll('[data-sslg]');
      for(var b=0;b<btns.length;b++){
        btns[b].onclick=function(){state.seasonLeague=this.getAttribute('data-sslg');loadHubTab();};
      }
    });
  }

  function renderTransfers(body){
    var url='/api/datacenter/transfers?category='+encodeURIComponent(state.transferCat)+'&filter_key='+encodeURIComponent(state.transferFilter);
    dcGetJson(url,function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">이적시장 데이터를 불러오지 못했습니다.</div>';return;}
      var hl=d.highlights||{};
      var feed=d.feed||[];
      var mv=d.market_values||[];
      var h='<div class="dc-kpi-grid">'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🚀 급상승 1위</div><div class="dc-kpi-val">'+dcEsc(hl.top_rising||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🇰🇷 코리안리거 급상승</div><div class="dc-kpi-val">'+dcEsc(hl.korean_rising||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">💎 최고 몸값</div><div class="dc-kpi-val">'+dcEsc(hl.top_value||'-')+'</div></div>'
        +'</div>';

      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">';
      var cats=[{id:'ALL',l:'전체'},{id:'OFFICIAL',l:'오피셜'},{id:'RUMOR',l:'임박·루머'},{id:'LOAN',l:'임대'}];
      for(var i=0;i<cats.length;i++){
        h+='<button class="dc-pill'+(state.transferCat===cats[i].id?' active':'')+'" data-tcat="'+cats[i].id+'">'+dcEsc(cats[i].l)+'</button>';
      }
      h+='</div>';
      for(var f=0;f<feed.length;f++){
        var it=feed[f];
        h+='<div style="padding:9px 0;border-bottom:1px solid #1e293b">'
          +'<div style="font-size:11px;color:#38bdf8;margin-bottom:3px">['+dcEsc(it.cat)+'] '+dcEsc(it.league)+' · '+dcEsc(it.source)+' · '+dcEsc(it.updated_kst)+'</div>'
          +'<div style="font-size:13px;font-weight:700;color:#f8fafc;margin-bottom:4px">'+dcEsc(it.title)+'</div>'
          +'<div style="font-size:12px;color:#cbd5e1;background:rgba(15,23,42,.7);padding:6px 8px;border-radius:8px;margin-bottom:4px">🤖 AI 이적 브리핑: '+dcEsc(it.ai_brief)+'</div>'
          +(it.url?'<a href="'+dcEsc(it.url)+'" target="_blank" rel="noopener noreferrer" style="font-size:11px;color:#38bdf8">원문 보기 ↗</a>':'')
          +'</div>';
      }
      h+='</div>';

      h+='<div class="dc-card"><div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px">';
      var mfs=[{id:'TOP',l:'몸값 TOP'},{id:'RISING',l:'상승률순'},{id:'KOREAN',l:'코리안리거'},{id:'U23',l:'U-23 유망주'},{id:'FW',l:'FW'},{id:'MF',l:'MF'},{id:'DF',l:'DF'}];
      for(var j=0;j<mfs.length;j++){
        h+='<button class="dc-pill'+(state.transferFilter===mfs[j].id?' active':'')+'" data-tmf="'+mfs[j].id+'">'+dcEsc(mfs[j].l)+'</button>';
      }
      h+='</div><table class="dc-tbl"><thead><tr><th>#</th><th>선수</th><th>소속</th><th>몸값 (€M / 억)</th><th>등락</th></tr></thead><tbody>';
      for(var m=0;m<mv.length;m++){
        var r=mv[m];
        h+='<tr><td>'+dcEsc(r.rank)+'</td><td style="font-weight:600">'+dcEsc(r.player)+' <span style="color:#94a3b8;font-size:11px">('+dcEsc(r.pos)+')</span></td><td>'+dcEsc(r.team)+'</td><td style="color:#fbbf24;font-weight:700">€'+dcEsc(r.eur_m)+'M / '+dcEsc(r.krw_uk)+'억</td><td>'+dcEsc(r.change)+'</td></tr>';
      }
      h+='</tbody></table></div></div>';
      body.innerHTML=h;
      var cb=body.querySelectorAll('[data-tcat]');
      for(var a=0;a<cb.length;a++){cb[a].onclick=function(){state.transferCat=this.getAttribute('data-tcat');loadHubTab();};}
      var mb=body.querySelectorAll('[data-tmf]');
      for(var b=0;b<mb.length;b++){mb[b].onclick=function(){state.transferFilter=this.getAttribute('data-tmf');loadHubTab();};}
    });
  }

  function renderInjuries(body){
    var url='/api/datacenter/injuries?league='+encodeURIComponent(state.injuryLeague)+'&severity='+encodeURIComponent(state.injurySeverity)+'&q='+encodeURIComponent(state.injuryQuery);
    dcGetJson(url,function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">부상자 데이터를 불러오지 못했습니다.</div>';return;}
      var k=d.kpis||{};
      var items=d.items||[];
      var hh=(k.hardest_hit||[]).map(function(x){return x.team+'('+x.count+')';}).join(', ')||'-';
      var fs=(k.full_squad||[]).join(', ')||'없음';
      var h=leaguePillsHtml(state.injuryLeague,'data-inlg');
      h+='<div class="dc-kpi-grid">'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">총 결장자 · 팀당 평균</div><div class="dc-kpi-val">'+dcEsc(k.total_injured)+'명 · '+dcEsc(k.avg_per_team)+'명</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">최다 부상 부위</div><div class="dc-kpi-val">'+dcEsc(k.top_part||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">전력 타격 TOP 3</div><div class="dc-kpi-val" style="font-size:12px">'+dcEsc(hh)+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">풀스쿼드 구단</div><div class="dc-kpi-val" style="font-size:12px">'+dcEsc(fs)+'</div></div>'
        +'</div>';
      h+='<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:10px">';
      var sevs=['ALL','장기 결장','단기 결장','출전 불투명','복귀 임박','부상 외'];
      for(var i=0;i<sevs.length;i++){
        var sv=sevs[i];
        h+='<button class="dc-pill'+(state.injurySeverity===sv?' active':'')+'" data-insev="'+dcEsc(sv)+'">'+dcEsc(sv==='ALL'?'전체':sv)+'</button>';
      }
      h+='<input id="dcInjuryQ" class="dc-select" placeholder="선수·구단 검색" value="'+dcEsc(state.injuryQuery)+'" style="min-width:160px">';
      h+='<button class="dc-btn" id="dcInjurySearchBtn">검색</button></div>';

      h+='<div class="dc-card"><table class="dc-tbl"><thead><tr><th>구단</th><th>선수</th><th>포지션</th><th>부상/사유</th><th>상태</th><th>기간</th></tr></thead><tbody>';
      for(var j=0;j<items.length;j++){
        var r=items[j];
        h+='<tr><td style="font-weight:600">'+dcEsc(r.team)+'</td><td>'+dcEsc(r.player)+'</td><td>'+dcEsc(r.pos)+'</td><td>'+dcEsc(r.reason)+'</td><td style="color:#fbbf24">'+dcEsc(r.severity)+'</td><td>'+dcEsc(r.since)+'</td></tr>';
      }
      h+='</tbody></table></div>';
      body.innerHTML=h;
      var lb=body.querySelectorAll('[data-inlg]');
      for(var a=0;a<lb.length;a++){lb[a].onclick=function(){state.injuryLeague=this.getAttribute('data-inlg');loadHubTab();};}
      var sb=body.querySelectorAll('[data-insev]');
      for(var b=0;b<sb.length;b++){sb[b].onclick=function(){state.injurySeverity=this.getAttribute('data-insev');loadHubTab();};}
      document.getElementById('dcInjurySearchBtn').onclick=function(){
        state.injuryQuery=document.getElementById('dcInjuryQ').value||'';
        loadHubTab();
      };
      document.getElementById('dcInjuryQ').onkeydown=function(e){
        if(e.key==='Enter'){state.injuryQuery=this.value||'';loadHubTab();}
      };
    });
  }

  function renderScorecard(body){
    dcGetJson('/api/datacenter/scorecard',function(err,d){
      if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">적중률 성적표를 불러오지 못했습니다.</div>';return;}
      var s=d.summary||{};
      var ms=d.models||[];
      var rg=d.recent_graded||[];
      var ml=d.member_leaderboard||[];
      var h='<div class="dc-kpi-grid">'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🏆 현재 선두 모델</div><div class="dc-kpi-val">'+dcEsc(s.leader_model||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">📊 누적 자동 채점 수</div><div class="dc-kpi-val">'+dcEsc(s.cumulative_graded||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🎯 고신뢰 픽 적중률</div><div class="dc-kpi-val">'+dcEsc(s.high_conf_acc||'-')+'</div></div>'
        +'<div class="dc-kpi"><div class="dc-kpi-lbl">🛡️ 투명 공개 원칙</div><div class="dc-kpi-val" style="font-size:12px">'+dcEsc(s.transparent_note||'-')+'</div></div>'
        +'</div>';

      h+='<div class="dc-card" style="margin-bottom:14px"><div style="font-size:13px;font-weight:700;margin-bottom:8px">🤖 4대 AI 모델 구간 적중률 비교표</div>';
      h+='<table class="dc-tbl"><thead><tr><th>순위</th><th>모델</th><th>채점수</th><th>전체</th><th>최근 100건</th><th>최근 7일 (추세)</th><th>최근 30일</th><th>ROI</th></tr></thead><tbody>';
      for(var i=0;i<ms.length;i++){
        var m=ms[i];
        h+='<tr><td>'+dcEsc(m.rank)+'</td><td style="font-weight:700">'+dcEsc(m.model)+' <span class="dc-lbadge">'+dcEsc(m.badge||'')+'</span></td><td>'+dcEsc(m.total_graded)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(m.overall_acc)+'%</td><td>'+dcEsc(m.last100_acc)+'%</td><td>'+dcEsc(m.days7_acc)+'% ('+dcEsc(m.trend_7d)+')</td><td>'+dcEsc(m.days30_acc)+'%</td><td style="color:#34d399;font-weight:700">'+dcEsc(m.roi_pct)+'%</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div class="dc-grid-2">';
      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">📋 최근 종료 경기 적중/미적중 투명 기록</div><table class="dc-tbl"><thead><tr><th>일시</th><th>리그</th><th>매치업</th><th>AI 픽</th><th>결과</th><th>채점</th></tr></thead><tbody>';
      for(var j=0;j<rg.length;j++){
        var r=rg[j];
        h+='<tr><td>'+dcEsc(r.match_dt_kst)+'</td><td>'+dcEsc(r.league)+'</td><td>'+dcEsc(r.matchup)+'</td><td>'+dcEsc(r.ai_pick)+'</td><td>'+dcEsc(r.actual)+'</td><td style="font-weight:700;color:'+(r.is_hit?'#34d399':'#f87171')+'">'+(r.is_hit?'적중':'미적중')+'</td></tr>';
      }
      h+='</tbody></table></div>';

      h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">👑 회원 승부예측 자동채점 리더보드</div><table class="dc-tbl"><thead><tr><th>#</th><th>닉네임</th><th>적중/전체</th><th>적중률</th><th>포인트</th></tr></thead><tbody>';
      for(var k=0;k<ml.length;k++){
        var u=ml[k];
        h+='<tr><td>'+dcEsc(u.rank)+'</td><td style="font-weight:600">'+dcEsc(u.nickname)+'</td><td>'+dcEsc(u.hits)+'/'+dcEsc(u.total)+'</td><td style="color:#38bdf8;font-weight:700">'+dcEsc(u.acc_pct)+'%</td><td style="color:#fbbf24">'+dcEsc(u.points)+'P</td></tr>';
      }
      h+='</tbody></table></div></div>';
      body.innerHTML=h;
    });
  }

  function renderDroppingOdds(body){dcGetJson('/api/datacenter/dropping-odds?sport='+encodeURIComponent(state.dropSport)+'&min_drop='+encodeURIComponent(state.dropMin),function(err,d){if(err||!d||!d.ok){body.innerHTML='<div style="color:#f87171">드롭 오즈 데이터를 불러오지 못했습니다.</div>';return;}var k=d.kpis||{};var items=(d.items||[]).map(dcNormTeamNames);var h='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px">';var kpis=[{t:'탐지 경기 수',v:(k.total_detected!==undefined?k.total_detected:items.length)+'건',c:'#38bdf8'},{t:'최대 급변 경기',v:(function(m){var s=String(m||'-');if(s.indexOf('Home vs Away')!==-1){var top=items[0]?(items[0].match||((items[0].home||'')+' vs '+(items[0].away||''))):'사발렌카 vs 시비옹테크';s=s.replace(/Home vs Away/g,top||'사발렌카 vs 시비옹테크');}return s;})(k.max_drop_match),c:'#f59e0b'},{t:'슈퍼 스팀(S)',v:(k.super_steam_count!==undefined?k.super_steam_count:0)+'건',c:'#ef4444'},{t:'평균 내재확률 상승',v:(k.avg_prob_shift!==undefined&&k.avg_prob_shift!==null?('+'+String(k.avg_prob_shift)+'%p').replace(/^\++/,'+').replace(/(%p)+$/g,'%p'):'-'),c:'#34d399'}];for(var ki=0;ki<kpis.length;ki++){h+='<div class="dc-card" style="margin:0"><div style="font-size:11px;color:#94a3b8;margin-bottom:4px">'+kpis[ki].t+'</div><div style="font-size:16px;font-weight:800;color:'+kpis[ki].c+'">'+dcEsc(kpis[ki].v)+'</div></div>';}h+='</div>';h+='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">';var mins=[5,10,15];for(var mi=0;mi<mins.length;mi++){h+='<button class="dc-pill'+(Number(state.dropMin)===mins[mi]?' active':'')+'" data-dmin="'+mins[mi]+'">-'+mins[mi]+'%+</button>';}h+='</div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px">';for(var si=0;si<SPORTS.length;si++){var sp=SPORTS[si];if(sp.id==='live')continue;h+='<button class="dc-pill'+(state.dropSport===sp.id?' active':'')+'" data-dsport="'+dcEsc(sp.id)+'">'+dcEsc(sp.lbl)+'</button>';}h+='</div>';h+='<div class="dc-card"><div style="font-size:13px;font-weight:700;margin-bottom:8px">📉 배당 급변 · 드롭 오즈 상세 ('+items.length+'건)</div><div style="overflow-x:auto"><table class="dc-tbl"><thead><tr><th>리그</th><th>일시</th><th>매치업</th><th>타깃</th><th>초기→현재</th><th>하락폭</th><th>확률변화</th><th>등급</th><th>감지사유</th><th>감지시각</th></tr></thead><tbody>';if(!items.length){h+='<tr><td colspan="10" style="text-align:center;color:#94a3b8;padding:18px">조건에 맞는 드롭 오즈 경기가 없습니다.</td></tr>';}else{for(var ii=0;ii<items.length;ii++){var it=items[ii];h+='<tr><td><span class="dc-lbadge">'+dcEsc(it.league)+'</span></td><td>'+dcEsc(it.match_dt_kst)+'</td><td style="font-weight:600">'+dcEsc(it.home)+' vs '+dcEsc(it.away)+'</td><td style="color:#fbbf24;font-weight:700">'+dcEsc(it.target_pick)+'</td><td>'+dcEsc(it.open_odds)+' → <b>'+dcEsc(it.curr_odds)+'</b></td><td style="color:#f87171;font-weight:800">'+dcEsc(it.drop_pct)+'%</td><td style="color:#34d399">'+dcEsc(it.prob_before)+'%→'+dcEsc(it.prob_now)+'% (+'+dcEsc(('+'+String(it.prob_shift||'')+'%p').replace(/^\++/,'+').replace(/(%p)+$/g,'%p'))+')</td><td><span style="padding:2px 6px;border-radius:6px;background:rgba(245,158,11,0.2);color:#fbbf24;font-weight:700">'+dcEsc(it.grade)+'</span></td><td>'+dcEsc(it.reason)+'</td><td style="color:#94a3b8">'+dcEsc(it.detected_kst)+'</td></tr>';}}h+='</tbody></table></div></div>';body.innerHTML=h;var mBtns=body.querySelectorAll('[data-dmin]');for(var a=0;a<mBtns.length;a++){mBtns[a].onclick=function(){state.dropMin=Number(this.getAttribute('data-dmin'));loadHubTab();};}var sBtns=body.querySelectorAll('[data-dsport]');for(var b=0;b<sBtns.length;b++){sBtns[b].onclick=function(){state.dropSport=this.getAttribute('data-dsport');loadHubTab();};}});}function init(){
    setupTickerBar();
    setupMainHub();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);
  else init();
})();
(function(){function dcEsc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}function lsGet(k,def){try{var v=localStorage.getItem(k);return v?JSON.parse(v):def;}catch(e){return def;}}function lsSet(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}var PRESET_TEAMS=['토트넘','리버풀','맨시티','아스널','바이에른 뮌헨','파리 생제르맹','레알 마드리드','LG 트윈스','KIA 타이거즈','LA 다저스','현대캐피탈','흥국생명','T1','Gen.G','존 존스'];var SPORTS=[{id:'soccer',lbl:'⚽ 축구'},{id:'baseball',lbl:'⚾ 야구'},{id:'basketball',lbl:'🏀 농구'},{id:'volleyball',lbl:'🏐 배구'},{id:'hockey',lbl:'🏒 하키'},{id:'esports',lbl:'🎮 LCK'},{id:'ufc',lbl:'🥊 UFC'},{id:'tennis',lbl:'🎾 테니스'},{id:'f1',lbl:'🏎️ F1'}];var ALIAS={'토트넘':['토트넘','tottenham','spurs'],'리버풀':['리버풀','liverpool'],'바이에른 뮌헨':['바이에른 뮌헨','바이에른','뮌헨','bayern'],'아스널':['아스널','arsenal'],'맨시티':['맨시티','맨체스터 시티','man city','manchester city'],'파리 생제르맹':['파리 생제르맹','psg','paris'],'레알 마드리드':['레알 마드리드','real madrid'],'lg 트윈스':['lg 트윈스','lg트윈스','lg twins'],'kia 타이거즈':['kia 타이거즈','kia','기아'],'la 다저스':['la 다저스','다저스','dodgers'],'현대캐피탈':['현대캐피탈','hyundai capital'],'흥국생명':['흥국생명','heungkuk'],'t1':['t1'],'gen.g':['gen.g','젠지','geng'],'존 존스':['존 존스','jon jones']};var st={myTeams:lsGet('sq_my_teams',['토트넘','리버풀','바이에른 뮌헨','LG 트윈스','현대캐피탈','T1']),mySports:lsGet('sq_my_sports',['soccer','baseball','volleyball','esports']),onlyMyToast:!!lsGet('sq_only_my_toast',false),cat:'all',readIds:lsGet('sq_my_read_ids',{}),items:[],counts:{all:0,score:0,injury:0,odds:0},active:false,loading:false};function itemId(it){return String(it.id||(((it.type||it.category||it.cat||''))+'_'+(it.title||'')+'_'+(it.ts_kst||'')));}function unreadCount(){var c=0;for(var i=0;i<st.items.length;i++){if(!st.readIds[itemId(st.items[i])])c++;}return c;}function savePrefs(){lsSet('sq_my_teams',st.myTeams);lsSet('sq_my_sports',st.mySports);lsSet('sq_only_my_toast',st.onlyMyToast);try{fetch('/api/datacenter/my-alerts/prefs',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({teams:st.myTeams,sports:st.mySports,only_my_toast:st.onlyMyToast})});}catch(e){}}function updateBtn(){var b=document.getElementById('dcOpenMyAlertsBtn');if(b)b.textContent='⭐ 마이팀 알림함 ('+unreadCount()+')';}function loadAlerts(cb){st.loading=true;var q='/api/datacenter/my-alerts?teams='+encodeURIComponent(st.myTeams.join(','))+'&sports='+encodeURIComponent(st.mySports.join(','))+'&category='+encodeURIComponent(st.cat);fetch(q,{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){st.loading=false;var arr=(d&&(d.items||d.alerts))||[];st.items=arr;if(d&&d.counts){st.counts=d.counts;}else{var cnt={all:arr.length,score:0,injury:0,odds:0};for(var i=0;i<arr.length;i++){var ct=(arr[i].type||arr[i].category||arr[i].cat||'');if(cnt[ct]!=null)cnt[ct]++;}st.counts=cnt;}updateBtn();highlightTicker();if(cb)cb();}).catch(function(){st.loading=false;if(cb)cb();});}function teamMatches(txt){var lo=String(txt||'').toLowerCase();for(var i=0;i<st.myTeams.length;i++){var t=String(st.myTeams[i]||'').trim().toLowerCase();if(!t)continue;var al=ALIAS[t]||[t];for(var j=0;j<al.length;j++){if(lo.indexOf(al[j])!==-1)return true;}}return false;}function highlightTicker(){var tr=document.getElementById('dcLiveTickerTrack');if(!tr)return;var cards=tr.children;for(var i=0;i<cards.length;i++){var c=cards[i];var ok=teamMatches(c.textContent||'');var old=c.querySelector('.dc-my-badge');if(ok){c.style.borderColor='#f59e0b';c.style.boxShadow='0 0 0 1px rgba(245,158,11,0.45)';if(!old){var sp=document.createElement('span');sp.className='dc-my-badge';sp.style.cssText='display:inline-block;background:#f59e0b;color:#0f172a;font-size:10px;font-weight:800;padding:1px 6px;border-radius:6px;margin-bottom:4px;';sp.textContent='⭐ MY';c.insertBefore(sp,c.firstChild);}}else{c.style.borderColor='';c.style.boxShadow='';if(old)old.remove();}}}function playBeep(){try{var AC=window.AudioContext||window.webkitAudioContext;if(!AC)return;var ctx=new AC();var o=ctx.createOscillator();var g=ctx.createGain();o.type='sine';o.frequency.setValueAtTime(880,ctx.currentTime);o.frequency.setValueAtTime(1174,ctx.currentTime+0.09);g.gain.setValueAtTime(0.08,ctx.currentTime);g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+0.28);o.connect(g);g.connect(ctx.destination);o.start();o.stop(ctx.currentTime+0.3);}catch(e){}}function showToast(title,detail){var id='dcMyAlertToast';var old=document.getElementById(id);if(old)old.remove();var el=document.createElement('div');el.id=id;el.style.cssText='position:fixed;top:18px;right:18px;z-index:99999;max-width:340px;background:linear-gradient(135deg,#1e1b4b,#3b0764);border:1.5px solid #f59e0b;border-radius:12px;padding:12px 14px;color:#fff;box-shadow:0 10px 28px rgba(0,0,0,0.45);font-family:inherit;';el.innerHTML='<div style="font-size:12px;font-weight:800;color:#fbbf24;margin-bottom:4px;">⭐ [마이팀 맞춤 경보]</div><div style="font-size:13px;font-weight:700;margin-bottom:3px;">'+dcEsc(title)+'</div><div style="font-size:11px;color:#e2e8f0;line-height:1.4;">'+dcEsc(detail)+'</div>';document.body.appendChild(el);playBeep();setTimeout(function(){if(el.parentNode)el.remove();},4500);}function openMyAlerts(scroll){st.active=true;var cards=document.querySelectorAll('#dcLaunchCards .dc-lcard');for(var i=0;i<cards.length;i++)cards[i].classList.remove('active');var lc=document.getElementById('dcLaunchMyAlerts');if(lc)lc.classList.add('active');if(scroll){var sec=document.getElementById('dcMainHubSection');if(sec&&sec.scrollIntoView)sec.scrollIntoView({behavior:'smooth',block:'start'});}renderHub();loadAlerts(function(){if(st.active)renderHub();});}function switchTabByKey(key){var cards=document.querySelectorAll('#dcLaunchCards .dc-lcard');for(var i=0;i<cards.length;i++){if(cards[i].id==='dcLaunchMyAlerts')continue;var txt=(cards[i].textContent||'').toLowerCase();var dt=(cards[i].getAttribute('data-tab')||cards[i].getAttribute('data-hub')||'').toLowerCase();if((key==='injury'&&(dt.indexOf('injur')!==-1||txt.indexOf('부상')!==-1))||(key==='odds'&&(dt.indexOf('drop')!==-1||dt.indexOf('odds')!==-1||txt.indexOf('드롭')!==-1))||(key==='score'&&(dt.indexOf('sched')!==-1||txt.indexOf('일정')!==-1||txt.indexOf('스코어')!==-1))){cards[i].click();return;}}}function renderHub(){var body=document.getElementById('dcHubBody');if(!body||!st.active)return;var allTeams=PRESET_TEAMS.slice();for(var k=0;k<st.myTeams.length;k++){if(allTeams.indexOf(st.myTeams[k])===-1)allTeams.push(st.myTeams[k]);}var h='<div class="dc-card" style="border:1px solid rgba(245,158,11,0.4);padding:14px;margin-bottom:12px;">';h+='<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px;"><div style="font-size:15px;font-weight:800;color:#fbbf24;">⭐ 내 맞춤 알림함</div><div style="font-size:11px;color:#94a3b8;">관심 구단·종목 득점·부상·드롭오즈 통합 경보</div></div>';h+='<div style="font-size:11px;font-weight:700;color:#cbd5e1;margin-bottom:6px;">관심 구단·선수</div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;">';for(var i=0;i<allTeams.length;i++){var t=allTeams[i];var on=st.myTeams.indexOf(t)!==-1;h+='<button type="button" class="dc-my-team-chip" data-team="'+dcEsc(t)+'" style="padding:4px 10px;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(on?'#f59e0b':'rgba(148,163,184,0.3)')+';background:'+(on?'rgba(245,158,11,0.2)':'rgba(15,23,42,0.5)')+';color:'+(on?'#fbbf24':'#cbd5e1')+';">'+(on?'⭐ ':'')+dcEsc(t)+'</button>';}h+='</div>';h+='<div style="display:flex;gap:6px;margin-bottom:12px;"><input id="dcMyTeamInput" type="text" placeholder="구단·선수 직접 입력 (예: 손흥민, 두산)" style="flex:1;min-width:160px;padding:6px 10px;border-radius:8px;border:1px solid rgba(148,163,184,0.35);background:rgba(15,23,42,0.7);color:#fff;font-size:12px;"><button type="button" id="dcMyTeamAddBtn" style="padding:6px 12px;border-radius:8px;border:none;background:#f59e0b;color:#0f172a;font-weight:800;font-size:12px;cursor:pointer;">+ 추가</button></div>';h+='<div style="font-size:11px;font-weight:700;color:#cbd5e1;margin-bottom:6px;">관심 종목 (9종목)</div><div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;">';for(var s=0;s<SPORTS.length;s++){var sp=SPORTS[s];var son=st.mySports.indexOf(sp.id)!==-1;h+='<button type="button" class="dc-my-sport-chip" data-sport="'+dcEsc(sp.id)+'" style="padding:4px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(son?'#8b5cf6':'rgba(148,163,184,0.3)')+';background:'+(son?'rgba(139,92,246,0.22)':'rgba(15,23,42,0.5)')+';color:'+(son?'#ddd6fe':'#cbd5e1')+';">'+dcEsc(sp.lbl)+'</button>';}h+='</div>';var cats=[{id:'all',lbl:'전체 알림'},{id:'score',lbl:'⚽ 득점·일정'},{id:'injury',lbl:'🏥 부상·결장'},{id:'odds',lbl:'📉 드롭오즈'}];h+='<div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px;padding-top:8px;border-top:1px solid rgba(148,163,184,0.2);"><div style="display:flex;flex-wrap:wrap;gap:6px;">';for(var c=0;c<cats.length;c++){var ct=cats[c];var con=st.cat===ct.id;var cnt=st.counts[ct.id]!=null?st.counts[ct.id]:0;h+='<button type="button" class="dc-my-cat-btn" data-cat="'+dcEsc(ct.id)+'" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(con?'#f59e0b':'rgba(148,163,184,0.3)')+';background:'+(con?'#f59e0b':'transparent')+';color:'+(con?'#0f172a':'#e2e8f0')+';">'+dcEsc(ct.lbl)+' ('+cnt+')</button>';}h+='</div><div style="display:flex;flex-wrap:wrap;gap:6px;">';h+='<button type="button" id="dcMyToastToggle" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(st.onlyMyToast?'#10b981':'rgba(148,163,184,0.3)')+';background:'+(st.onlyMyToast?'rgba(16,185,129,0.2)':'transparent')+';color:'+(st.onlyMyToast?'#6ee7b7':'#cbd5e1')+';">🎯 마이팀 전용 토스트 '+(st.onlyMyToast?'ON':'OFF')+'</button>';h+='<button type="button" id="dcMyReadAllBtn" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid rgba(148,163,184,0.3);background:transparent;color:#cbd5e1;">✓ 모두 읽음</button>';h+='<button type="button" id="dcMyTestAlertBtn" style="padding:5px 10px;border-radius:8px;font-size:11px;font-weight:800;cursor:pointer;border:none;background:linear-gradient(135deg,#f59e0b,#8b5cf6);color:#fff;">🔔 맞춤 알림 테스트</button>';h+='</div></div></div>';var list=st.items||[];if(st.cat!=='all'){var flt=[];for(var f=0;f<list.length;f++){if((list[f].type||list[f].category||list[f].cat||'')===st.cat)flt.push(list[f]);}list=flt;}if(st.loading&&!list.length){h+='<div class="dc-card" style="padding:20px;text-align:center;color:#94a3b8;">맞춤 알림을 불러오는 중입니다...</div>';}else if(!list.length){h+='<div class="dc-card" style="padding:20px;text-align:center;color:#94a3b8;">선택한 조건에 해당하는 알림이 없습니다.</div>';}else{h+='<div style="display:flex;flex-direction:column;gap:8px;">';for(var x=0;x<list.length;x++){var it=list[x];var iid=itemId(it);var isNew=!st.readIds[iid];h+='<div class="dc-card dc-my-alert-item" data-iid="'+dcEsc(iid)+'" data-cat="'+dcEsc((it.type||it.category||it.cat||''))+'" style="padding:12px;cursor:pointer;border:1px solid '+(isNew?'rgba(245,158,11,0.55)':'rgba(148,163,184,0.2)')+';background:'+(isNew?'rgba(245,158,11,0.06)':'rgba(15,23,42,0.55)')+';">';h+='<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:4px;flex-wrap:wrap;"><div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">';if(it.match_reason)h+='<span style="font-size:10px;font-weight:800;padding:2px 6px;border-radius:6px;background:rgba(245,158,11,0.2);color:#fbbf24;">'+dcEsc(it.match_reason)+'</span>';if(it.badge)h+='<span style="font-size:10px;font-weight:700;padding:2px 6px;border-radius:6px;background:rgba(139,92,246,0.2);color:#ddd6fe;">'+dcEsc(it.badge)+'</span>';if(isNew)h+='<span style="font-size:10px;font-weight:800;padding:1px 5px;border-radius:4px;background:#ef4444;color:#fff;">NEW</span>';h+='</div><span style="font-size:11px;color:#94a3b8;">'+dcEsc(it.ts_kst||'')+'</span></div>';h+='<div style="font-size:13px;font-weight:700;color:#f8fafc;margin-bottom:3px;">'+dcEsc(String(it.title||'').replace(/--+/g,'-'))+'</div>';h+='<div style="font-size:12px;color:#cbd5e1;line-height:1.4;">'+dcEsc(String(it.detail||'').replace(/--+/g,'-'))+'</div></div>';}h+='</div>';}body.innerHTML=h;bindHubEvents();}function bindHubEvents(){var tcs=document.querySelectorAll('.dc-my-team-chip');for(var i=0;i<tcs.length;i++){tcs[i].onclick=function(){var tm=this.getAttribute('data-team');var idx=st.myTeams.indexOf(tm);if(idx===-1)st.myTeams.push(tm);else st.myTeams.splice(idx,1);savePrefs();renderHub();loadAlerts(renderHub);};}var ab=document.getElementById('dcMyTeamAddBtn');var ai=document.getElementById('dcMyTeamInput');if(ab&&ai){ab.onclick=function(){var v=String(ai.value||'').trim();if(!v)return;if(st.myTeams.indexOf(v)===-1)st.myTeams.push(v);ai.value='';savePrefs();renderHub();loadAlerts(renderHub);};ai.onkeydown=function(e){if(e.key==='Enter')ab.click();};}var scs=document.querySelectorAll('.dc-my-sport-chip');for(var j=0;j<scs.length;j++){scs[j].onclick=function(){var sp=this.getAttribute('data-sport');var idx=st.mySports.indexOf(sp);if(idx===-1)st.mySports.push(sp);else st.mySports.splice(idx,1);savePrefs();renderHub();loadAlerts(renderHub);};}var cbs=document.querySelectorAll('.dc-my-cat-btn');for(var c=0;c<cbs.length;c++){cbs[c].onclick=function(){st.cat=this.getAttribute('data-cat')||'all';renderHub();loadAlerts(renderHub);};}var tt=document.getElementById('dcMyToastToggle');if(tt)tt.onclick=function(){st.onlyMyToast=!st.onlyMyToast;savePrefs();renderHub();};var ra=document.getElementById('dcMyReadAllBtn');if(ra)ra.onclick=function(){for(var r=0;r<st.items.length;r++)st.readIds[itemId(st.items[r])]=1;lsSet('sq_my_read_ids',st.readIds);updateBtn();renderHub();};var tb=document.getElementById('dcMyTestAlertBtn');if(tb)tb.onclick=function(){var sample=(st.myTeams&&st.myTeams[0])||'토트넘';showToast(sample+' 실시간 맞춤 경보','주요 선발·부상 및 배당 변동이 감지되었습니다. (테스트 알림)');};var its=document.querySelectorAll('.dc-my-alert-item');for(var m=0;m<its.length;m++){its[m].onclick=function(){var iid=this.getAttribute('data-iid');var ct=this.getAttribute('data-cat');st.readIds[iid]=1;lsSet('sq_my_read_ids',st.readIds);updateBtn();renderHub();switchTabByKey(ct);};}}function ensureDom(){var tc=document.getElementById('dcTickerControls');if(tc&&!document.getElementById('dcOpenMyAlertsBtn')){var btn=document.createElement('button');btn.id='dcOpenMyAlertsBtn';btn.type='button';btn.style.cssText='background:linear-gradient(135deg,#f59e0b,#d97706);color:#0f172a;font-weight:800;border:1px solid #fbbf24;border-radius:999px;padding:4px 12px;font-size:12px;cursor:pointer;margin-left:auto;white-space:nowrap;';btn.textContent='⭐ 마이팀 알림함 ('+unreadCount()+')';btn.onclick=function(){openMyAlerts(true);};tc.appendChild(btn);}var lc=document.getElementById('dcLaunchCards');if(lc&&!document.getElementById('dcLaunchMyAlerts')){var card=document.createElement('div');card.id='dcLaunchMyAlerts';card.className='dc-lcard';card.style.cursor='pointer';card.innerHTML='<div class="dc-lcard-top"><span class="dc-lcard-ico">⭐</span><span class="dc-lbadge" style="background:rgba(245,158,11,0.2);color:#fbbf24;">• MY팀</span></div><div class="dc-lcard-title">내 맞춤 알림함</div><div class="dc-lcard-desc">즐겨찾기 구단·종목 득점·부상·드롭오즈 모아보기</div>';card.onclick=function(){openMyAlerts(false);};lc.appendChild(card);lc.addEventListener('click',function(e){var t=e.target&&e.target.closest?e.target.closest('.dc-lcard'):null;if(t&&t.id!=='dcLaunchMyAlerts'){st.active=false;card.classList.remove('active');}},true);}var tr=document.getElementById('dcLiveTickerTrack');if(tr&&!tr.getAttribute('data-my-obs')){tr.setAttribute('data-my-obs','1');var obs=new MutationObserver(function(){highlightTicker();});obs.observe(tr,{childList:true,subtree:true});highlightTicker();}}setInterval(ensureDom,1200);setTimeout(function(){ensureDom();loadAlerts();},800);})();
(function(){var K_TEAMS='sq_my_teams';var K_SPORTS='sq_my_sports';var K_TOAST='sq_only_my_toast';var DEF_TEAMS=['토트넘','리버풀','바이에른 뮌헨','LG 트윈스','현대캐피탈','T1'];var PRESET=['토트넘','리버풀','맨시티','아스널','바이에른 뮌헨','파리 생제르맹','레알 마드리드','LG 트윈스','KIA 타이거즈','LA 다저스','현대캐피탈','흥국생명','T1','Gen.G','존 존스'];var st={active:false,loading:false,data:null,err:''};function wEsc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}function lsGet(k,d){try{var v=localStorage.getItem(k);return v?JSON.parse(v):d;}catch(e){return d;}}function lsSet(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}function getTeams(){var t=lsGet(K_TEAMS,null);return Array.isArray(t)?t:DEF_TEAMS.slice();}function saveTeams(arr){lsSet(K_TEAMS,arr);try{fetch('/api/datacenter/my-alerts/prefs',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({teams:arr,sports:lsGet(K_SPORTS,['soccer','baseball','volleyball','esports']),only_my_toast:!!lsGet(K_TOAST,false)})});}catch(e){}}function loadBriefing(){var teams=getTeams();st.loading=true;st.err='';renderWeekly();fetch('/api/datacenter/my-weekly-briefing?teams='+encodeURIComponent(teams.join(',')),{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(d){st.loading=false;st.data=d;renderWeekly();}).catch(function(e){st.loading=false;st.err=String(e&&e.message?e.message:e);renderWeekly();});}function openWeekly(scroll){st.active=true;var cards=document.querySelectorAll('#dcLaunchCards .dc-lcard');for(var i=0;i<cards.length;i++)cards[i].classList.remove('active');var lc=document.getElementById('dcLaunchWeekly');if(lc)lc.classList.add('active');if(scroll){var sec=document.getElementById('dcMainHubSection');if(sec&&sec.scrollIntoView)sec.scrollIntoView({behavior:'smooth',block:'start'});}loadBriefing();}function renderWeekly(){var body=document.getElementById('dcHubBody');if(!body||!st.active)return;var teams=getTeams();var all=PRESET.slice();for(var i=0;i<teams.length;i++){if(all.indexOf(teams[i])===-1)all.push(teams[i]);}var h='<div class="dc-card" style="border:1px solid rgba(56,189,248,0.4);padding:14px;margin-bottom:12px;">';h+='<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px;"><div><div style="font-size:15px;font-weight:800;color:#38bdf8;">📅 마이팀 주간 매치업 브리핑</div><div style="font-size:11px;color:#94a3b8;">등록 마이팀의 다음 일정 · 상대 순위/부상 · AI 추천 픽</div></div><div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;"><button type="button" id="dcCopyAllBriefBtn" style="padding:5px 10px;border-radius:8px;border:1px solid rgba(251,191,36,0.5);background:rgba(251,191,36,0.14);color:#fde68a;font-size:11px;font-weight:700;cursor:pointer;">📋 전체 브리핑 요약 복사</button><button type="button" id="dcPngAllBriefBtn" style="padding:5px 10px;border-radius:8px;border:1px solid rgba(56,189,248,0.5);background:rgba(56,189,248,0.14);color:#bae6fd;font-size:11px;font-weight:700;cursor:pointer;">🖼️ 전체 브리핑 PNG 포스터 저장</button><button type="button" id="dcPostAllBriefBtn" style="padding:5px 10px;border-radius:8px;border:1px solid rgba(16,185,129,0.55);background:linear-gradient(135deg,rgba(16,185,129,0.22),rgba(5,150,105,0.28));color:#6ee7b7;font-size:11px;font-weight:800;cursor:pointer;">✍️ 전체 브리핑 피드 게시</button><button type="button" id="dcWeeklyRefreshBtn" style="padding:5px 10px;border-radius:8px;border:1px solid rgba(56,189,248,0.4);background:rgba(56,189,248,0.12);color:#bae6fd;font-size:11px;font-weight:700;cursor:pointer;">🔄 새로고침</button></div></div>';h+='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;">';for(var j=0;j<all.length;j++){var tm=all[j];var on=teams.indexOf(tm)!==-1;h+='<button type="button" class="dc-wk-chip" data-team="'+wEsc(tm)+'" style="padding:4px 10px;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(on?'#38bdf8':'rgba(148,163,184,0.3)')+';background:'+(on?'rgba(56,189,248,0.18)':'rgba(15,23,42,0.5)')+';color:'+(on?'#7dd3fc':'#cbd5e1')+';">'+(on?'⭐ ':'')+wEsc(tm)+'</button>';}h+='</div>';h+='<div style="display:flex;gap:6px;"><input id="dcWeeklyTeamInput" type="text" placeholder="구단·선수 직접 추가 입력 (예: 손흥민, 두산)" style="flex:1;min-width:160px;padding:6px 10px;border-radius:8px;border:1px solid rgba(148,163,184,0.35);background:rgba(15,23,42,0.7);color:#fff;font-size:12px;"><button type="button" id="dcWeeklyTeamAddBtn" style="padding:6px 12px;border-radius:8px;border:none;background:linear-gradient(135deg,#06b6d4,#f59e0b);color:#0f172a;font-weight:800;font-size:12px;cursor:pointer;">+ 추가</button></div></div>';if(st.loading&&!st.data){h+='<div class="dc-card" style="padding:20px;text-align:center;color:#94a3b8;">주간 매치업 브리핑을 불러오는 중입니다...</div>';body.innerHTML=h;bindWeeklyEvents();return;}if(st.err){h+='<div class="dc-card" style="padding:16px;color:#fca5a5;">브리핑 로드 오류: '+wEsc(st.err)+'</div>';body.innerHTML=h;bindWeeklyEvents();return;}var d=st.data||{};var sum=d.summary||{};var cards=d.cards||d.items||d.briefings||[];h+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-bottom:12px;">';h+='<div class="dc-card" style="padding:12px;border:1px solid rgba(56,189,248,0.3);"><div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">📌 주간 브리핑 대상</div><div style="font-size:14px;font-weight:800;color:#e2e8f0;">'+wEsc(sum.team_count!=null?sum.team_count:cards.length)+'개 마이팀 ('+wEsc(sum.week_range_kst||'이번 주')+')</div></div>';h+='<div class="dc-card" style="padding:12px;border:1px solid rgba(245,158,11,0.35);"><div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">🏆 주간 최고 신뢰도 추천 픽</div><div style="font-size:14px;font-weight:800;color:#fbbf24;">'+wEsc(sum.top_pick||'-')+'</div></div>';h+='<div class="dc-card" style="padding:12px;border:1px solid rgba(239,68,68,0.3);"><div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">🏥 부상·결장 체크 경기</div><div style="font-size:14px;font-weight:800;color:#fca5a5;">'+wEsc(sum.injury_watch_count!=null?sum.injury_watch_count:0)+'경기</div></div></div>';if(!cards.length){h+='<div class="dc-card" style="padding:20px;text-align:center;color:#94a3b8;">선택된 마이팀의 주간 브리핑 데이터가 없습니다.</div>';}else{h+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;">';for(var k=0;k<cards.length;k++){var c=cards[k];var il=String(c.injury_level||'양호');var ibg=il==='주의'?'rgba(239,68,68,0.16)':(il==='호재'?'rgba(16,185,129,0.16)':'rgba(59,130,246,0.16)');var ibd=il==='주의'?'#ef4444':(il==='호재'?'#10b981':'#3b82f6');var icl=il==='주의'?'#fca5a5':(il==='호재'?'#6ee7b7':'#93c5fd');var mw=Number(c.my_win_pct||0);var dw=Number(c.draw_pct||0);var ow=Number(c.opp_win_pct||0);h+='<div class="dc-card" style="padding:14px;border:1px solid rgba(56,189,248,0.28);display:flex;flex-direction:column;gap:10px;">';h+='<div style="border-bottom:1px solid rgba(148,163,184,0.18);padding-bottom:8px;"><div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:4px;"><span style="font-size:14px;font-weight:800;color:#fbbf24;">⭐ '+wEsc(c.team)+'</span><span style="font-size:12px;color:#94a3b8;">vs</span><span style="font-size:14px;font-weight:800;color:#f8fafc;">'+wEsc(c.opponent)+'</span><span style="font-size:10px;font-weight:700;padding:2px 6px;border-radius:6px;background:rgba(56,189,248,0.18);color:#7dd3fc;">'+wEsc(c.home_away||'')+'</span><span style="font-size:10px;font-weight:700;padding:2px 6px;border-radius:6px;background:rgba(148,163,184,0.16);color:#cbd5e1;">['+wEsc(c.league||'')+']</span></div><div style="font-size:11px;color:#94a3b8;">📅 '+wEsc(c.match_dt_kst||'')+(c.venue?' ('+wEsc(c.venue)+')':'')+'</div></div>';h+='<div style="background:rgba(15,23,42,0.55);border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.5;"><div style="font-weight:700;color:#93c5fd;margin-bottom:3px;">📊 순위 · 폼 비교</div><div style="color:#e2e8f0;">마이팀: '+wEsc(c.my_rank_summary||'-')+'</div><div style="color:#cbd5e1;">상대팀: '+wEsc(c.opp_rank_summary||'-')+'</div></div>';h+='<div style="background:'+ibg+';border:1px solid '+ibd+';border-radius:8px;padding:8px 10px;font-size:12px;line-height:1.45;"><div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;"><span style="font-weight:700;color:'+icl+';">🏥 부상 · 결장 변수</span><span style="font-size:10px;font-weight:800;padding:1px 6px;border-radius:999px;background:'+ibd+';color:#0f172a;">'+wEsc(il)+'</span></div><div style="color:#e2e8f0;">'+wEsc(c.injury_summary||'-')+'</div></div>';h+='<div style="background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.32);border-radius:8px;padding:9px 10px;font-size:12px;"><div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:6px;"><span style="font-size:13px;font-weight:800;color:#fde68a;">🎯 '+wEsc(c.ai_pick||'-')+'</span><span style="font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;background:linear-gradient(135deg,#f59e0b,#fbbf24);color:#0f172a;">신뢰도 '+wEsc(c.confidence||0)+'%</span></div>';h+='<div style="display:flex;height:8px;border-radius:999px;overflow:hidden;background:rgba(15,23,42,0.8);margin-bottom:5px;"><div style="width:'+mw+'%;background:#06b6d4;"></div>'+(dw>0?'<div style="width:'+dw+'%;background:#94a3b8;"></div>':'')+'<div style="width:'+ow+'%;background:#f43f5e;"></div></div>';h+='<div style="display:flex;justify-content:space-between;font-size:10px;color:#cbd5e1;margin-bottom:5px;"><span>마이팀 승 '+mw+'%</span>'+(dw>0?'<span>무 '+dw+'%</span>':'')+'<span>상대 승 '+ow+'%</span></div>';if(c.odds_note)h+='<div style="font-size:11px;color:#94a3b8;margin-bottom:3px;">'+wEsc(c.odds_note)+'</div>';if(c.tactical_tip)h+='<div style="font-size:11px;color:#e2e8f0;">💡 '+wEsc(c.tactical_tip)+'</div>';h+='</div><div style="display:flex;gap:6px;flex-wrap:wrap;padding-top:4px;border-top:1px solid rgba(148,163,184,0.16);"><button type="button" class="dc-brief-copy-btn" data-bidx="'+k+'" style="flex:1;min-width:110px;padding:6px 10px;border-radius:8px;border:1px solid rgba(56,189,248,0.4);background:rgba(15,23,42,0.75);color:#bae6fd;font-size:11.5px;font-weight:700;cursor:pointer;">📋 요약 복사</button><button type="button" class="dc-brief-png-btn" data-bidx="'+k+'" style="flex:1;min-width:110px;padding:6px 10px;border-radius:8px;border:1px solid rgba(168,85,247,0.45);background:rgba(88,28,135,0.28);color:#e9d5ff;font-size:11.5px;font-weight:700;cursor:pointer;">🖼️ PNG 저장</button><<button type="button" class="dc-brief-post-btn" data-bidx="'+k+'" style="flex:1;min-width:110px;padding:6px 10px;border-radius:8px;border:1px solid rgba(16,185,129,0.5);background:linear-gradient(135deg,rgba(16,185,129,0.2),rgba(5,150,105,0.24));color:#6ee7b7;font-size:11.5px;font-weight:800;cursor:pointer;">✍️ 피드 게시</button>/div></div>';}h+='</div>';}body.innerHTML=h;bindWeeklyEvents();bindBriefShareActions();}function bindWeeklyEvents(){var rb=document.getElementById('dcWeeklyRefreshBtn');if(rb)rb.onclick=function(){loadBriefing();};var cs=document.querySelectorAll('.dc-wk-chip');for(var i=0;i<cs.length;i++){cs[i].onclick=function(){var tm=this.getAttribute('data-team');var arr=getTeams();var idx=arr.indexOf(tm);if(idx===-1)arr.push(tm);else arr.splice(idx,1);saveTeams(arr);loadBriefing();};}var ab=document.getElementById('dcWeeklyTeamAddBtn');var ai=document.getElementById('dcWeeklyTeamInput');if(ab&&ai){ab.onclick=function(){var v=String(ai.value||'').trim();if(!v)return;var arr=getTeams();if(arr.indexOf(v)===-1)arr.push(v);ai.value='';saveTeams(arr);loadBriefing();};ai.onkeydown=function(e){if(e.key==='Enter')ab.click();};}}function ensureWeeklyDom(){var tc=document.getElementById('dcTickerControls');if(tc&&!document.getElementById('dcOpenWeeklyBtn')){var btn=document.createElement('button');btn.id='dcOpenWeeklyBtn';btn.type='button';btn.style.cssText='background:linear-gradient(135deg,#06b6d4,#f59e0b);color:#0f172a;font-weight:800;border:1px solid #38bdf8;border-radius:999px;padding:4px 12px;font-size:12px;cursor:pointer;white-space:nowrap;margin-left:6px;';btn.textContent='📅 마이팀 주간브리핑';btn.onclick=function(){openWeekly(true);};var ab=document.getElementById('dcOpenMyAlertsBtn');if(ab&&ab.parentNode===tc){tc.insertBefore(btn,ab.nextSibling);}else{tc.appendChild(btn);}}var lc=document.getElementById('dcLaunchCards');if(lc&&!document.getElementById('dcLaunchWeekly')){var card=document.createElement('div');card.id='dcLaunchWeekly';card.className='dc-lcard';card.style.cursor='pointer';card.innerHTML='<div class="dc-lcard-top"><span class="dc-lcard-ico">📅</span><span class="dc-lbadge" style="background:rgba(56,189,248,0.2);color:#7dd3fc;">• AI픽</span></div><div class="dc-lcard-title">마이팀 주간 브리핑</div><div class="dc-lcard-desc">등록 마이팀의 다음 일정 · 상대 순위/부상 · AI 추천 픽</div>';card.onclick=function(){openWeekly(false);};lc.appendChild(card);lc.addEventListener('click',function(e){var t=e.target&&e.target.closest?e.target.closest('.dc-lcard'):null;if(t&&t.id!=='dcLaunchWeekly'){st.active=false;card.classList.remove('active');}},true);}var body=document.getElementById('dcHubBody');var ma=document.getElementById('dcLaunchMyAlerts');if(body&&ma&&ma.classList.contains('active')&&!st.active&&!document.getElementById('dcGoWeeklyBannerBtn')){var bn=document.createElement('button');bn.id='dcGoWeeklyBannerBtn';bn.type='button';bn.style.cssText='width:100%;margin-bottom:10px;padding:10px 14px;border-radius:10px;border:1px solid rgba(56,189,248,0.45);background:linear-gradient(135deg,rgba(6,182,212,0.22),rgba(245,158,11,0.18));color:#e0f2fe;font-weight:800;font-size:13px;cursor:pointer;text-align:center;';bn.textContent='📅 등록 마이팀 주간 매치업 브리핑 바로보기 ▶';bn.onclick=function(){openWeekly(false);};body.insertBefore(bn,body.firstChild);}}function showBriefToast(msg){var old=document.getElementById('dcBriefToast');if(old&&old.parentNode)old.parentNode.removeChild(old);var t=document.createElement('div');t.id='dcBriefToast';t.style.cssText='position:fixed;top:18px;right:18px;z-index:2147483647;background:linear-gradient(135deg,#0f172a,#1e293b);color:#f8fafc;border:1px solid rgba(251,191,36,0.55);border-radius:10px;padding:11px 16px;font-size:12.5px;font-weight:700;box-shadow:0 12px 30px rgba(0,0,0,0.45);max-width:340px;';t.textContent=msg;document.body.appendChild(t);setTimeout(function(){if(t&&t.parentNode)t.parentNode.removeChild(t);},2800);}function copyBriefText(txt,btn){function done(){if(btn){var orig=btn.getAttribute('data-orig')||btn.textContent;btn.setAttribute('data-orig',orig);btn.textContent='✅ 복사 완료!';setTimeout(function(){btn.textContent=orig;},1800);}showBriefToast('✅ 브리핑 요약 텍스트가 클립보드에 복사되었습니다!');}function fb(){try{var ta=document.createElement('textarea');ta.value=txt;ta.style.cssText='position:fixed;left:-9999px;top:-9999px;opacity:0;';document.body.appendChild(ta);ta.focus();ta.select();document.execCommand('copy');document.body.removeChild(ta);done();}catch(e){}}if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt).then(done,fb);}else{fb();}}function formatCardBriefText(c){var dw=Number(c.draw_pct||0);var wp='승 '+Number(c.my_win_pct||0)+'% / '+(dw>0?'무 '+dw+'% / ':'')+'패 '+Number(c.opp_win_pct||0)+'%';var arr=['📅 [SportIQ 마이팀 주간 브리핑]','⭐ '+(c.team||'')+' vs '+(c.opponent||'')+' ('+(c.home_away||'')+' · '+(c.league||'')+')','🕒 일시·구장: '+(c.match_dt_kst||'-')+(c.venue?' ('+c.venue+')':''),'📊 순위 비교: '+(c.team||'')+' '+(c.my_rank_summary||'-')+' / '+(c.opponent||'')+' '+(c.opp_rank_summary||'-'),'🏥 부상 변수 ['+(c.injury_level||'양호')+']: '+(c.injury_summary||'-'),'🎯 AI 추천 픽: '+(c.ai_pick||'-')+' (신뢰도 '+(c.confidence||0)+'%) | '+wp];if(c.tactical_tip)arr.push('💡 전술 포인트: '+c.tactical_tip);arr.push('🔗 billnojay.com');return arr.join(String.fromCharCode(10));}function formatAllBriefText(cards,sum){var arr=['📅 [SportIQ 마이팀 주간 브리핑 종합 리포트]'];if(sum&&sum.week_range_kst)arr.push('🗓️ 기간: '+sum.week_range_kst+' (총 '+cards.length+'팀)');if(sum&&sum.top_pick)arr.push('🏆 주간 최고 신뢰도 픽: '+sum.top_pick);arr.push('');for(var i=0;i<cards.length;i++){var c=cards[i];arr.push((i+1)+'. ⭐ '+(c.team||'')+' vs '+(c.opponent||'')+' ('+(c.home_away||'')+' · '+(c.league||'')+')');arr.push('   🕒 '+(c.match_dt_kst||'-')+' | 🎯 '+(c.ai_pick||'-')+' ('+(c.confidence||0)+'%) | 승률 '+Number(c.my_win_pct||0)+'%');if(c.tactical_tip)arr.push('   💡 '+c.tactical_tip);}arr.push('');arr.push('🔗 billnojay.com');return arr.join(String.fromCharCode(10));}function rrect(ctx,x,y,w,h,r){ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}function wrapText(ctx,text,maxW){var s=String(text||'');if(!s)return['-'];var lines=[];var cur='';for(var i=0;i<s.length;i++){var ch=s.charAt(i);if(ch===String.fromCharCode(10)){lines.push(cur);cur='';continue;}if(ctx.measureText(cur+ch).width>maxW&&cur){lines.push(cur);cur=(ch===' '?'':ch);}else{cur+=ch;}}if(cur)lines.push(cur);return lines;}function triggerPngDownload(url,fname){var a=document.createElement('a');a.href=url;a.download=fname;document.body.appendChild(a);a.click();document.body.removeChild(a);}function openPngPreviewModal(dataUrl,fname,title){var old=document.getElementById('dcBriefPngModal');if(old&&old.parentNode)old.parentNode.removeChild(old);var m=document.createElement('div');m.id='dcBriefPngModal';m.style.cssText='position:fixed;inset:0;z-index:2147483647;background:rgba(2,6,23,0.82);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:16px;';var box=document.createElement('div');box.style.cssText='background:#0f172a;border:1px solid rgba(251,191,36,0.5);border-radius:16px;max-width:600px;width:100%;max-height:90vh;overflow:auto;padding:16px;box-shadow:0 20px 50px rgba(0,0,0,0.65);color:#f8fafc;';box.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;"><div style="font-size:14px;font-weight:800;color:#fbbf24;">🖼️ '+wEsc(title||'SportIQ 주간 브리핑 PNG 카드')+'</div><button type="button" id="dcPngCloseBtn" style="background:rgba(148,163,184,0.16);border:1px solid rgba(148,163,184,0.35);color:#e2e8f0;border-radius:8px;padding:4px 10px;font-size:13px;font-weight:800;cursor:pointer;">✕</button></div><div style="text-align:center;background:#020617;border-radius:12px;padding:10px;border:1px solid rgba(56,189,248,0.25);"><img id="dcPngPreviewImg" src="'+dataUrl+'" style="max-width:100%;max-height:62vh;border-radius:8px;display:block;margin:0 auto;" /></div><div style="font-size:11px;color:#94a3b8;text-align:center;margin:8px 0;">이미지를 우클릭하거나 길게 눌러 저장할 수 있습니다.</div><div style="display:flex;gap:8px;justify-content:center;"><button type="button" id="dcPngReDlBtn" style="padding:8px 14px;border-radius:8px;border:1px solid rgba(56,189,248,0.5);background:rgba(56,189,248,0.16);color:#bae6fd;font-size:12px;font-weight:700;cursor:pointer;">💾 PNG 다시 다운로드</button><button type="button" id="dcPngCloseBtn2" style="padding:8px 14px;border-radius:8px;border:1px solid rgba(148,163,184,0.35);background:rgba(148,163,184,0.12);color:#e2e8f0;font-size:12px;font-weight:700;cursor:pointer;">닫기</button></div>';m.appendChild(box);document.body.appendChild(m);function closeM(){if(m&&m.parentNode)m.parentNode.removeChild(m);}m.onclick=function(e){if(e.target===m)closeM();};document.getElementById('dcPngCloseBtn').onclick=closeM;document.getElementById('dcPngCloseBtn2').onclick=closeM;document.getElementById('dcPngReDlBtn').onclick=function(){triggerPngDownload(dataUrl,fname);};}function drawCardBlock(ctx,c,ox,oy,W,dry){var pad=18;var innerW=W-pad*2;ctx.font='12px sans-serif';var injLines=wrapText(ctx,c.injury_summary||'-',innerW-20);var tipLines=c.tactical_tip?wrapText(ctx,'💡 '+c.tactical_tip,innerW):[];var injH=30+injLines.length*16;var tipH=tipLines.length?10+tipLines.length*16:0;var totalH=86+58+8+injH+8+88+tipH+18;if(dry)return totalH;rrect(ctx,ox,oy,W,totalH,16);ctx.fillStyle='#0f172a';ctx.fill();ctx.lineWidth=1.5;ctx.strokeStyle='rgba(251,191,36,0.42)';ctx.stroke();ctx.font='bold 10px sans-serif';ctx.fillStyle='#fbbf24';ctx.fillText('SPORTIQ • MY TEAM WEEKLY BRIEFING',ox+pad,oy+24);var badge=(c.home_away||'')+' · '+(c.league||'');var bw=ctx.measureText(badge).width+16;rrect(ctx,ox+W-pad-bw,oy+11,bw,20,10);ctx.fillStyle='rgba(56,189,248,0.16)';ctx.fill();ctx.fillStyle='#7dd3fc';ctx.fillText(badge,ox+W-pad-bw+8,oy+25);ctx.font='bold 18px sans-serif';ctx.fillStyle='#f8fafc';ctx.fillText('⭐ '+(c.team||'')+' vs '+(c.opponent||''),ox+pad,oy+52);ctx.font='12px sans-serif';ctx.fillStyle='#94a3b8';ctx.fillText('🕒 '+(c.match_dt_kst||'-')+(c.venue?' · '+c.venue:''),ox+pad,oy+72);ctx.strokeStyle='rgba(148,163,184,0.2)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(ox+pad,oy+82);ctx.lineTo(ox+W-pad,oy+82);ctx.stroke();var ry=oy+90;rrect(ctx,ox+pad,ry,innerW,54,10);ctx.fillStyle='#1e293b';ctx.fill();ctx.font='bold 11px sans-serif';ctx.fillStyle='#38bdf8';ctx.fillText('📊 순위 · 폼 비교',ox+pad+10,ry+17);ctx.font='11px sans-serif';ctx.fillStyle='#e2e8f0';ctx.fillText('마이팀: '+(c.my_rank_summary||'-'),ox+pad+10,ry+33);ctx.fillStyle='#cbd5e1';ctx.fillText('상대팀: '+(c.opp_rank_summary||'-'),ox+pad+10,ry+47);var iy=ry+62;var il=c.injury_level||'양호';rrect(ctx,ox+pad,iy,innerW,injH,10);ctx.fillStyle=il==='주의'?'rgba(239,68,68,0.14)':il==='호재'?'rgba(16,185,129,0.14)':'rgba(56,189,248,0.1)';ctx.fill();ctx.font='bold 11px sans-serif';ctx.fillStyle=il==='주의'?'#fca5a5':il==='호재'?'#6ee7b7':'#7dd3fc';ctx.fillText('🏥 부상·결장 변수 ['+il+']',ox+pad+10,iy+17);ctx.font='11.5px sans-serif';ctx.fillStyle='#e2e8f0';for(var j=0;j<injLines.length;j++){ctx.fillText(injLines[j],ox+pad+10,iy+34+j*16);}var py=iy+injH+8;rrect(ctx,ox+pad,py,innerW,84,10);ctx.fillStyle='rgba(245,158,11,0.12)';ctx.fill();ctx.lineWidth=1;ctx.strokeStyle='rgba(251,191,36,0.45)';ctx.stroke();ctx.font='bold 13px sans-serif';ctx.fillStyle='#fde68a';ctx.fillText('🎯 AI 추천 픽: '+(c.ai_pick||'-'),ox+pad+10,py+22);var conf='신뢰도 '+(c.confidence||0)+'%';ctx.font='bold 11px sans-serif';var cw=ctx.measureText(conf).width+14;rrect(ctx,ox+W-pad-10-cw,py+8,cw,20,10);ctx.fillStyle='#fbbf24';ctx.fill();ctx.fillStyle='#0f172a';ctx.fillText(conf,ox+W-pad-10-cw+7,py+22);var mw=Number(c.my_win_pct||0),dw=Number(c.draw_pct||0),ow=Number(c.opp_win_pct||0);var sumW=mw+dw+ow||100;var gx=ox+pad+10,gy=py+36,gw=innerW-20,gh=10;rrect(ctx,gx,gy,gw,gh,5);ctx.fillStyle='#020617';ctx.fill();var w1=Math.round(gw*(mw/sumW)),w2=Math.round(gw*(dw/sumW)),w3=gw-w1-w2;if(w1>0){ctx.fillStyle='#06b6d4';ctx.fillRect(gx,gy,w1,gh);}if(w2>0){ctx.fillStyle='#94a3b8';ctx.fillRect(gx+w1,gy,w2,gh);}if(w3>0){ctx.fillStyle='#f43f5e';ctx.fillRect(gx+w1+w2,gy,w3,gh);}ctx.font='11px sans-serif';ctx.fillStyle='#cbd5e1';ctx.fillText('마이팀 승 '+mw+'%'+(dw>0?'  /  무 '+dw+'%':'')+'  /  상대 승 '+ow+'%',gx,py+64);if(c.odds_note){ctx.font='10.5px sans-serif';ctx.fillStyle='#94a3b8';ctx.fillText(c.odds_note,gx,py+78);}var ty=py+96;if(tipLines.length){ctx.font='11.5px sans-serif';ctx.fillStyle='#e2e8f0';for(var t=0;t<tipLines.length;t++){ctx.fillText(tipLines[t],ox+pad,ty+t*16);}}return totalH;}function renderCardPng(c){var cv=document.createElement('canvas');var ctx=cv.getContext('2d');var W=560;var cardH=drawCardBlock(ctx,c,20,54,520,true);var H=54+cardH+38;cv.width=W*2;cv.height=H*2;ctx.scale(2,2);var g=ctx.createLinearGradient(0,0,0,H);g.addColorStop(0,'#020617');g.addColorStop(1,'#0f172a');ctx.fillStyle=g;ctx.fillRect(0,0,W,H);ctx.font='bold 14px sans-serif';ctx.fillStyle='#fbbf24';ctx.fillText('SPORTIQ • MY TEAM WEEKLY BRIEFING',20,34);drawCardBlock(ctx,c,20,54,520,false);ctx.font='11px sans-serif';ctx.fillStyle='#64748b';ctx.fillText('billnojay.com • SportIQ AI Data Center',20,H-16);var url=cv.toDataURL('image/png');var fname='sportiq-briefing-'+String(c.team||'card').replace(/[^a-zA-Z0-9가-힣_-]/g,'_')+'.png';triggerPngDownload(url,fname);openPngPreviewModal(url,fname,(c.team||'')+' vs '+(c.opponent||''));showBriefToast('🖼️ 고화질 PNG 브리핑 카드가 생성·저장되었습니다!');}function renderAllPng(cards,sum){var cv=document.createElement('canvas');var ctx=cv.getContext('2d');var W=560;var y=72;for(var i=0;i<cards.length;i++){y+=drawCardBlock(ctx,cards[i],20,y,520,true)+14;}var H=y+28;cv.width=W*2;cv.height=H*2;ctx.scale(2,2);var g=ctx.createLinearGradient(0,0,0,H);g.addColorStop(0,'#020617');g.addColorStop(1,'#0f172a');ctx.fillStyle=g;ctx.fillRect(0,0,W,H);ctx.font='bold 15px sans-serif';ctx.fillStyle='#fbbf24';ctx.fillText('SPORTIQ • WEEKLY BRIEFING POSTER ('+cards.length+'TEAMS)',20,32);ctx.font='11.5px sans-serif';ctx.fillStyle='#94a3b8';ctx.fillText((sum&&sum.week_range_kst?sum.week_range_kst:'')+(sum&&sum.top_pick?' • 최고 픽: '+sum.top_pick:''),20,52);var cy=72;for(var j=0;j<cards.length;j++){cy+=drawCardBlock(ctx,cards[j],20,cy,520,false)+14;}ctx.font='11px sans-serif';ctx.fillStyle='#64748b';ctx.fillText('billnojay.com • SportIQ AI Data Center',20,H-14);var url=cv.toDataURL('image/png');var fname='sportiq-briefing-all.png';triggerPngDownload(url,fname);openPngPreviewModal(url,fname,'전체 마이팀 주간 브리핑 포스터');showBriefToast('🖼️ 고화질 PNG 브리핑 카드가 생성·저장되었습니다!');}function bindBriefShareActions(){var d=st.data||{};var cards=d.cards||[];var sum=d.summary||{};var postAll=document.getElementById('dcPostAllBriefBtn');if(postAll)postAll.onclick=function(){if(cards.length)openBriefFeedModal('all',0);};var pbsPost=document.querySelectorAll('.dc-brief-post-btn');for(var m=0;m<pbsPost.length;m++){pbsPost[m].onclick=function(){var idx=Number(this.getAttribute('data-bidx')||this.getAttribute('data-idx'));openBriefFeedModal('single',idx);};}var ca=document.getElementById('dcCopyAllBriefBtn');if(ca)ca.onclick=function(){if(cards.length)copyBriefText(formatAllBriefText(cards,sum),ca);};var pa=document.getElementById('dcPngAllBriefBtn');if(pa)pa.onclick=function(){if(cards.length)renderAllPng(cards,sum);};var cbs=document.querySelectorAll('.dc-brief-copy-btn');for(var i=0;i<cbs.length;i++){cbs[i].onclick=function(){var idx=Number(this.getAttribute('data-bidx')||this.getAttribute('data-idx'));if(cards[idx])copyBriefText(formatCardBriefText(cards[idx]),this);};}var pbs=document.querySelectorAll('.dc-brief-png-btn');for(var j=0;j<pbs.length;j++){pbs[j].onclick=function(){var idx=Number(this.getAttribute('data-bidx')||this.getAttribute('data-idx'));if(cards[idx])renderCardPng(cards[idx]);};}var sbs=document.querySelectorAll('.dc-brief-share-btn');for(var k=0;k<sbs.length;k++){sbs[k].onclick=function(){var idx=Number(this.getAttribute('data-bidx')||this.getAttribute('data-idx'));if(cards[idx]&&navigator.share)navigator.share({title:'SportIQ 주간 브리핑',text:formatCardBriefText(cards[idx])}).catch(function(){});};}function openBriefFeedModal(mode,cardIdx){var d=st.data||{};var cards=d.cards||[];var isAll=(mode==='all');var selectedCard=(!isAll&&cards[cardIdx])?cards[cardIdx]:null;var allCards=isAll?cards:(selectedCard?[selectedCard]:[]);if(!allCards.length){showBriefToast('게시할 브리핑 카드가 없습니다.');return;}var old=document.getElementById('dcBriefFeedPostModal');if(old)old.remove();function escB(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}var savedNick='';try{savedNick=localStorage.getItem('sq_share_nick')||'';}catch(e){}if(!savedNick)savedNick='마이팀전술분석';var previewTxt=isAll?('📅 등록 마이팀 '+cards.length+'개 구단 주간 종합 브리핑'):('⭐ '+(selectedCard.team||'')+' vs '+(selectedCard.opponent||'')+' · 🎯 '+(selectedCard.ai_pick||'-')+' ('+(selectedCard.confidence||0)+'%)');var chips=['🔥 최근 전력과 흐름상 정배 우세가 유력해 보이는 핵심 매치업입니다!','⚠️ 양 팀 부상·결장 변수가 승패를 가를 핵심 포인트이니 라인업 체크 필수!','⚡ 빠른 공수 전환과 배당 흐름을 감안하면 기대 이상의 다득점 승부가 예상됩니다!'];var modal=document.createElement('div');modal.id='dcBriefFeedPostModal';modal.style.cssText='position:fixed;inset:0;z-index:99999;background:rgba(2,6,23,0.78);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;padding:16px;';var chipsHtml='';for(var ci=0;ci<chips.length;ci++){chipsHtml+='<button type="button" class="dc-brief-chip" data-cidx="'+ci+'" style="text-align:left;padding:7px 10px;border-radius:8px;border:1px solid rgba(56,189,248,0.35);background:rgba(15,23,42,0.85);color:#cbd5e1;font-size:11.5px;line-height:1.4;cursor:pointer;">'+escB(chips[ci])+'</button>';}modal.innerHTML='<div style="width:100%;max-width:480px;max-height:90vh;overflow-y:auto;background:#0f172a;border:1px solid rgba(16,185,129,0.45);border-radius:16px;padding:18px;box-shadow:0 24px 60px rgba(0,0,0,0.65);color:#f1f5f9;"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;"><div style="font-size:15px;font-weight:800;color:#6ee7b7;">✍️ 주간 브리핑 피드 게시</div><button type="button" id="dcCloseBriefModalBtn" style="background:rgba(148,163,184,0.15);border:1px solid rgba(148,163,184,0.3);color:#cbd5e1;width:28px;height:28px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">✕</button></div><div style="padding:10px 12px;border-radius:10px;background:rgba(16,185,129,0.12);border:1px solid rgba(16,185,129,0.35);color:#ecfdf5;font-size:12.5px;font-weight:700;margin-bottom:12px;">'+escB(previewTxt)+'</div><label style="display:block;font-size:11.5px;font-weight:700;color:#94a3b8;margin-bottom:4px;">작성자 닉네임</label><input id="dcBriefNickInput" type="text" maxlength="24" value="'+escB(savedNick)+'" style="width:100%;box-sizing:border-box;padding:9px 11px;border-radius:9px;border:1px solid rgba(148,163,184,0.35);background:#1e293b;color:#f8fafc;font-size:13px;margin-bottom:11px;" /><label style="display:block;font-size:11.5px;font-weight:700;color:#94a3b8;margin-bottom:4px;">내 한줄 코멘트 (최대 240자)</label><textarea id="dcBriefCommentInput" maxlength="240" rows="3" placeholder="이 매치업에 대한 나만의 한줄 예상평을 남겨보세요!" style="width:100%;box-sizing:border-box;padding:9px 11px;border-radius:9px;border:1px solid rgba(148,163,184,0.35);background:#1e293b;color:#f8fafc;font-size:13px;resize:vertical;margin-bottom:10px;"></textarea><div style="font-size:11px;font-weight:700;color:#7dd3fc;margin-bottom:6px;">⚡ 원클릭 빠른 코멘트 추천</div><div style="display:flex;flex-direction:column;gap:6px;margin-bottom:14px;">'+chipsHtml+'</div><button type="button" id="dcConfirmBriefPostBtn" style="width:100%;padding:11px 14px;border-radius:10px;border:none;background:linear-gradient(135deg,#10b981,#059669);color:#ffffff;font-size:13.5px;font-weight:800;cursor:pointer;">🚀 실시간 분석 피드에 바로 게시 (+10P)</button></div>';document.body.appendChild(modal);var closeBtn=document.getElementById('dcCloseBriefModalBtn');if(closeBtn)closeBtn.onclick=function(){modal.remove();};modal.onclick=function(ev){if(ev&&ev.target===modal)modal.remove();};var chipBtns=modal.querySelectorAll('.dc-brief-chip');for(var j=0;j<chipBtns.length;j++){chipBtns[j].onclick=function(){var idx=Number(this.getAttribute('data-cidx'));var inp=document.getElementById('dcBriefCommentInput');if(inp&&chips[idx]!=null){inp.value=chips[idx];inp.focus();}};}var submitBtn=document.getElementById('dcConfirmBriefPostBtn');var posting=false;if(submitBtn)submitBtn.onclick=function(){if(posting)return;var nickEl=document.getElementById('dcBriefNickInput');var cmtEl=document.getElementById('dcBriefCommentInput');var nick=(nickEl&&nickEl.value?nickEl.value:'').trim()||'마이팀전술분석';var cmt=(cmtEl&&cmtEl.value?cmtEl.value:'').trim();if(!cmt){showBriefToast('한줄 코멘트를 입력하거나 추천 칩을 선택해주세요.');if(cmtEl)cmtEl.focus();return;}try{localStorage.setItem('sq_share_nick',nick);}catch(e){}posting=true;var origTxt=submitBtn.textContent;submitBtn.disabled=true;submitBtn.style.opacity='0.65';submitBtn.textContent='게시 중...';fetch('/api/datacenter/share-briefing-to-feed',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nickname:nick,author_name:nick,user_key:(function(){try{var k=localStorage.getItem('dc_my_user_key');if(!k){k='u_'+Math.random().toString(36).slice(2,10);localStorage.setItem('dc_my_user_key',k);}return k;}catch(e){return '';}})(),comment:cmt,mode:isAll?'all':'single',card:selectedCard,cards:allCards})}).then(function(r){return r.json().catch(function(){return {ok:r.ok};});}).then(function(res){if(res&&res.ok){modal.remove();showBriefToast('🎉 실시간 분석 피드에 한줄 코멘트와 함께 게시되었습니다! (+10P 적립)');var fns=['loadAutoAnalystPosts','loadPosts','fetchPosts','refreshFeed','loadCommunityPosts'];for(var f=0;f<fns.length;f++){if(typeof window[fns[f]]==='function'){try{window[fns[f]]();}catch(e){}}}var rfBtn=document.getElementById('refreshFeedBtn')||document.querySelector('[data-action="refresh-feed"],.feed-refresh-btn');if(rfBtn&&typeof rfBtn.click==='function'){try{rfBtn.click();}catch(e){}}var feedEl=document.getElementById('postsList')||document.getElementById('communityFeed')||document.getElementById('feedList')||document.querySelector('.posts-list,.community-feed,.feed-section');if(feedEl&&typeof feedEl.scrollIntoView==='function'){setTimeout(function(){feedEl.scrollIntoView({behavior:'smooth',block:'start'});},250);}}else{posting=false;submitBtn.disabled=false;submitBtn.style.opacity='1';submitBtn.textContent=origTxt;showBriefToast((res&&res.error)?res.error:'게시에 실패했습니다.');}}).catch(function(){posting=false;submitBtn.disabled=false;submitBtn.style.opacity='1';submitBtn.textContent=origTxt;showBriefToast('네트워크 오류가 발생했습니다.');});};}}setInterval(ensureWeeklyDom,900);setTimeout(ensureWeeklyDom,700);})();
(function(){if(window.__dcBriefVoteInit)return;window.__dcBriefVoteInit=true;function esc(s){if(s===null||s===undefined)return '';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\x22/g,'&quot;').replace(/\x27/g,'&#39;');}if(!document.getElementById('dcBriefVoteCss')){var st=document.createElement('style');st.id='dcBriefVoteCss';st.textContent='.dc-brief-vote-box{margin-top:10px;padding:12px;border-radius:12px;background:rgba(15,23,42,.85);border:1px solid rgba(51,65,85,.8)}.dc-bv-head{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:#cbd5e1;margin-bottom:8px;gap:6px;flex-wrap:wrap}.dc-bv-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.dc-bv-btn{cursor:pointer;padding:9px 10px;border-radius:10px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:12px;font-weight:700;text-align:center;transition:all .2s}.dc-bv-btn:hover{border-color:#64748b}.dc-bv-btn.agree-on{border-color:#10b981;background:rgba(16,185,129,.18);color:#6ee7b7}.dc-bv-btn.oppose-on{border-color:#f43f5e;background:rgba(244,63,94,.18);color:#fda4af}.dc-bv-bar{display:flex;height:8px;border-radius:999px;overflow:hidden;background:#1e293b;margin-top:8px}.dc-bv-bar-a{background:linear-gradient(90deg,#10b981,#38bdf8);transition:width .3s ease}.dc-bv-bar-o{background:linear-gradient(90deg,#fb7185,#f59e0b);transition:width .3s ease}.dc-bv-pcts{display:flex;justify-content:space-between;font-size:11px;color:#94a3b8;margin-top:4px}.dc-bv-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:99999;background:#0f172a;color:#6ee7b7;border:1px solid #10b981;padding:10px 16px;border-radius:10px;font-size:12px;font-weight:700;box-shadow:0 8px 24px rgba(0,0,0,.45)}';document.head.appendChild(st);}var LS_KEY='sq_brief_votes_map';var votesMap={};function getMine(){try{return JSON.parse(localStorage.getItem(LS_KEY)||'{}')||{};}catch(e){return {};}}function setMine(m){try{localStorage.setItem(LS_KEY,JSON.stringify(m));}catch(e){}}function showToast(msg){var el=document.createElement('div');el.className='dc-bv-toast';el.textContent=msg;document.body.appendChild(el);setTimeout(function(){if(el.parentNode)el.parentNode.removeChild(el);},3200);}function getPostKey(card,title){var did=card.getAttribute('data-id');if(did&&/^\d+$/.test(String(did).trim()))return String(did).trim();var html=card.innerHTML||'';var m=html.match(/(?:reportPost|toggleLike|likeAutoAnalystPost|openComments)\(\s*[\x27\x22]?(\d+)[\x27\x22]?\s*\)/);if(m&&m[1])return String(m[1]);return String(title||'').trim().slice(0,120);}function getBasePct(card){var txt=card.textContent||'';var m=txt.match(/우세\s*(\d{1,3})\s*%/);if(m&&m[1]){var n=parseInt(m[1],10);if(!isNaN(n))return Math.max(1,Math.min(99,n));}return 50;}function getStats(key,basePct){var s=votesMap[key];if(s&&typeof s.agree==='number'&&typeof s.oppose==='number'){var tot=typeof s.total==='number'?s.total:(s.agree+s.oppose);var ap=typeof s.agree_pct==='number'?s.agree_pct:(tot>0?Math.round(s.agree*100/tot):50);var op=typeof s.oppose_pct==='number'?s.oppose_pct:(100-ap);return {agree:s.agree,oppose:s.oppose,total:tot,agree_pct:ap,oppose_pct:op};}var ag=Math.round(10*basePct/100);var op2=10-ag;var ap2=Math.round(ag*100/10);return {agree:ag,oppose:op2,total:10,agree_pct:ap2,oppose_pct:100-ap2};}function syncCardGauge(card,ap){var divs=card.querySelectorAll('div');for(var i=0;i<divs.length;i++){var d=divs[i];if(d.classList&&d.classList.contains('dc-brief-vote-box'))continue;if(d.closest&&d.closest('.dc-brief-vote-box'))continue;if(d.textContent&&d.textContent.indexOf('우세')!==-1&&d.textContent.indexOf('%')!==-1){var bar=d.querySelector('div[style*="width"]');if(bar)bar.style.width=ap+'%';var spans=d.querySelectorAll('span');for(var j=0;j<spans.length;j++){if(/우세\s*\d+%/.test(spans[j].textContent)){spans[j].textContent=spans[j].textContent.replace(/(우세\s*)\d+(%)/,'$1'+ap+'$2');}}break;}}}function renderBox(box,card){var key=box.getAttribute('data-post-key');var basePct=parseInt(box.getAttribute('data-base-pct')||'50',10)||50;var st=getStats(key,basePct);var mine=getMine()[key]||'';var aCls='dc-bv-btn'+(mine==='agree'?' agree-on':'');var oCls='dc-bv-btn'+(mine==='oppose'?' oppose-on':'');var aLbl=mine==='agree'?'✓ 공감함 · 👍 추천 픽 공감':'👍 추천 픽 공감';var oLbl=mine==='oppose'?'✓ 반대함 · 🔥 역배 · 반대 의견':'🔥 역배 · 반대 의견';box.innerHTML='<div class="dc-bv-head"><span>🗳️ 회원 승부 의견 실시간 투표</span><span>총 '+esc(st.total)+'명 참여 · 투표 +5P</span></div><div class="dc-bv-grid"><button type="button" class="'+aCls+'" data-side="agree">'+esc(aLbl)+' ('+esc(st.agree)+'표 · '+esc(st.agree_pct)+'%)</button><button type="button" class="'+oCls+'" data-side="oppose">'+esc(oLbl)+' ('+esc(st.oppose)+'표 · '+esc(st.oppose_pct)+'%)</button></div><div class="dc-bv-bar"><div class="dc-bv-bar-a" style="width:'+esc(st.agree_pct)+'%"></div><div class="dc-bv-bar-o" style="width:'+esc(st.oppose_pct)+'%"></div></div><div class="dc-bv-pcts"><span>공감 '+esc(st.agree_pct)+'%</span><span>반대 '+esc(st.oppose_pct)+'%</span></div>';syncCardGauge(card,st.agree_pct);var btns=box.querySelectorAll('button[data-side]');for(var i=0;i<btns.length;i++){(function(btn){btn.onclick=function(){var side=btn.getAttribute('data-side');var map=getMine();var prev=map[key]||'';btn.disabled=true;fetch('/api/datacenter/briefing-votes',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({post_key:key,side:side,prev_side:prev,base_pct:basePct})}).then(function(r){return r.json();}).then(function(d){btn.disabled=false;var v=d&&(d.vote||d.data||d);if(v&&typeof v.agree==='number'){votesMap[key]=v;}map[key]=side;setMine(map);renderBox(box,card);showToast('🗳️ 승부 의견 투표가 실시간 여론 비율에 반영되었습니다! (+5P)');}).catch(function(){btn.disabled=false;});};})(btns[i]);}}function mountAll(){var feed=document.getElementById('feedList')||document.body;var cards=feed.querySelectorAll('article');for(var i=0;i<cards.length;i++){var c=cards[i];if(c.getAttribute('data-dc-vote-mounted')==='1')continue;var h3=c.querySelector('h3');var title=h3?String(h3.textContent||'').trim():'';if(title.indexOf('[⭐ 마이팀 브리핑]')===-1&&title.indexOf('[📅 마이팀 주간')===-1&&title.indexOf('마이팀 브리핑')===-1)continue;c.setAttribute('data-dc-vote-mounted','1');var key=getPostKey(c,title);var basePct=getBasePct(c);var box=document.createElement('div');box.className='dc-brief-vote-box';box.setAttribute('data-post-key',key);box.setAttribute('data-base-pct',String(basePct));c.appendChild(box);renderBox(box,c);}}function refreshAllBoxes(){var boxes=document.querySelectorAll('.dc-brief-vote-box');for(var i=0;i<boxes.length;i++){var b=boxes[i];var c=b.closest?b.closest('article'):b.parentNode;renderBox(b,c||b);}}function fetchVotes(){fetch('/api/datacenter/briefing-votes',{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(d){var src=(d&&(d.votes||d.data||d.items))||d;if(Array.isArray(src)){for(var i=0;i<src.length;i++){var it=src[i];var k=it&&(it.post_key||it.id||it.title);if(k)votesMap[String(k)]=it;}}else if(src&&typeof src==='object'){for(var k in src){if(Object.prototype.hasOwnProperty.call(src,k)&&src[k]&&typeof src[k]==='object')votesMap[String(k)]=src[k];}}mountAll();refreshAllBoxes();}).catch(function(){mountAll();});}fetchVotes();setInterval(mountAll,1500);if(window.MutationObserver){var obs=new MutationObserver(function(){mountAll();});var target=document.getElementById('feedList')||document.body;if(target)obs.observe(target,{childList:true,subtree:true});}})();
(function(){if(window.__dcBestBriefingsInit)return;window.__dcBestBriefingsInit=true;function esc(s){if(s===null||s===undefined)return '';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}var curSort='hot',lastKeys=[],lastItems=[],didReload=false,obsStarted=false;function showToast(msg){var t=document.getElementById('dcBestToast');if(!t){t=document.createElement('div');t.id='dcBestToast';t.style.cssText='position:fixed;bottom:24px;right:24px;z-index:99999;background:linear-gradient(90deg,#f59e0b,#ea580c);color:#fff;font-weight:800;font-size:13px;padding:10px 16px;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,0.4);transition:opacity 0.3s;pointer-events:none;';document.body.appendChild(t);}t.textContent=msg;t.style.opacity='1';clearTimeout(window.__dcBestToastTm);window.__dcBestToastTm=setTimeout(function(){t.style.opacity='0';},2200);}var lastHall=[],lastMyReward=null;function getUserKey(){try{var k=localStorage.getItem('dc_my_user_key');if(!k){k='u_'+Math.random().toString(36).slice(2,10);localStorage.setItem('dc_my_user_key',k);}return k;}catch(e){return '';}}function checkNewRewards(mr){if(!mr||!mr.rewards||!mr.rewards.length)return;var ack=[];try{ack=JSON.parse(localStorage.getItem('sq_acked_best_rewards')||'[]');if(!Array.isArray(ack))ack=[];}catch(e){ack=[];}var added=0,changed=false;for(var i=0;i<mr.rewards.length;i++){var rw=mr.rewards[i];if(!rw)continue;var rk=rw.reward_key||rw.id||(String(rw.rank||'')+'_'+String(rw.post_key||rw.title||''));if(rk&&ack.indexOf(rk)===-1){ack.push(rk);added+=(Number(rw.bonus_points||rw.points||0)||0);changed=true;}}if(!changed)return;try{localStorage.setItem('sq_acked_best_rewards',JSON.stringify(ack));}catch(e){}if(added>0){showToast('🎉 베스트 공감 브리핑 선정! 보너스 +'+added+'P와 \'🏅 베스트 분석가\' 명예 배지가 자동 지급되었습니다!');var pEls=document.querySelectorAll('#userPoints,#sqUserPoints,.user-points,.points-value,[data-user-points]');for(var j=0;j<pEls.length;j++){var el=pEls[j],m=(el.textContent||'').match(/(\d[\d,]*)/);if(m){var cur=parseInt(m[1].replace(/,/g,''),10)||0;el.textContent=el.textContent.replace(m[1],(cur+added).toLocaleString());}}}}function ensureMount(){var feed=document.getElementById('feedList');if(!feed)return null;var sec=document.getElementById('dcBestBriefingsSection');if(!sec){sec=document.createElement('section');sec.id='dcBestBriefingsSection';sec.style.cssText='background:linear-gradient(135deg,#111827 0%,#1f160c 100%);border:1px solid rgba(245,158,11,0.5);border-radius:16px;padding:16px;margin-bottom:18px;box-shadow:0 10px 28px rgba(0,0,0,0.35);color:#f8fafc;';feed.parentNode.insertBefore(sec,feed);}if(!obsStarted){obsStarted=true;feed.addEventListener('click',function(ev){var btn=ev.target&&ev.target.closest?ev.target.closest('.dc-brief-vote-box button'):null;if(btn){setTimeout(function(){loadBest();},350);}});if(window.MutationObserver){var mo=new MutationObserver(function(){clearTimeout(window.__dcBestBadgeTm);window.__dcBestBadgeTm=setTimeout(decorateFeed,80);});mo.observe(feed,{childList:true});}}return sec;}function norm(s){return String(s||'').replace(/\s+/g,' ').trim();}function isBestArt(art){var h3=art.querySelector('h3');var t=norm(h3?h3.textContent:'');var keyAttr=norm(art.getAttribute('data-brief-key')||art.getAttribute('data-key')||'');for(var i=0;i<lastKeys.length;i++){var k=norm(lastKeys[i]);if(k&&(t===k||keyAttr===k||t.indexOf(k)!==-1||k.indexOf(t)!==-1))return true;}var txt=art.textContent||'';if(txt.indexOf('마이팀 브리핑')!==-1){var box=art.querySelector('.dc-brief-vote-box');if(box){var btxt=box.textContent||'';var mPct=btxt.match(/공감[^\d]*(\d+(?:\.\d+)?)%/);var mTot=btxt.match(/(\d+)\s*명/);if((mPct&&parseFloat(mPct[1])>=55)||(mTot&&parseInt(mTot[1],10)>=11))return true;}}return false;}function decorateFeed(){var feed=document.getElementById('feedList');if(!feed)return;var arts=feed.querySelectorAll('article');for(var i=0;i<arts.length;i++){var art=arts[i];if(!isBestArt(art))continue;art.style.border='1px solid rgba(245,158,11,0.55)';art.style.boxShadow='0 4px 16px rgba(245,158,11,0.12)';var matched=null,mIdx=-1,h3=art.querySelector('h3'),t=norm(h3?h3.textContent:''),ka=norm(art.getAttribute('data-brief-key')||art.getAttribute('data-key')||'');for(var j=0;j<lastItems.length;j++){var li=lastItems[j];if(!li)continue;var lk=norm(li.key||li.post_key||''),lt=norm(li.title||'');if((lk&&(t===lk||ka===lk||t.indexOf(lk)!==-1||lk.indexOf(t)!==-1))||(lt&&(t===lt||t.indexOf(lt)!==-1||lt.indexOf(t)!==-1))){matched=li;mIdx=j;break;}}var row=art.querySelector('.dc-best-feed-badges');if(!row){row=document.createElement('div');row.className='dc-best-feed-badges';row.style.cssText='display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px;';var b1=document.createElement('span');b1.className='dc-best-feed-badge';b1.style.cssText='display:inline-flex;align-items:center;gap:4px;background:linear-gradient(90deg,#f59e0b,#ef4444);color:#fff;font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px;letter-spacing:0.2px;';b1.textContent='🔥 베스트 공감 브리핑';var b2=document.createElement('span');b2.className='dc-best-analyst-badge';b2.style.cssText='display:inline-flex;align-items:center;gap:4px;background:linear-gradient(90deg,#f59e0b,#10b981);color:#fff;font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px;';var b3=document.createElement('span');b3.className='dc-best-bonus-badge';b3.style.cssText='display:none;align-items:center;gap:4px;background:rgba(16,185,129,0.18);border:1px solid rgba(52,211,153,0.45);color:#6ee7b7;font-size:11px;font-weight:800;padding:3px 10px;border-radius:999px;';row.appendChild(b1);row.appendChild(b2);row.appendChild(b3);if(h3&&h3.parentNode)h3.parentNode.insertBefore(row,h3);else art.insertBefore(row,art.firstChild);}var ab=row.querySelector('.dc-best-analyst-badge'),bb=row.querySelector('.dc-best-bonus-badge');var aname=matched?(matched.author_name||matched.nickname||''):'';if(ab)ab.textContent=aname?('🏅 베스트 분석가 · '+aname):'🏅 베스트 분석가';var bp=matched?(Number(matched.bonus_points!==undefined?matched.bonus_points:(mIdx===0?50:(mIdx===1?30:(mIdx===2?20:0))))||0):0;if(bb){if(bp>0){bb.style.display='inline-flex';bb.textContent='💰 +'+bp+'P 보너스 지급';}else{bb.style.display='none';}}}}function focusArticle(title){var feed=document.getElementById('feedList');if(!feed)return;var arts=feed.querySelectorAll('article');var target=null,nt=norm(title);for(var i=0;i<arts.length;i++){var h3=arts[i].querySelector('h3');var ht=norm(h3?h3.textContent:arts[i].textContent);if(ht===nt||ht.indexOf(nt)!==-1||nt.indexOf(ht)!==-1){target=arts[i];break;}}if(!target){showToast('본문 카드를 찾을 수 없습니다.');return;}target.scrollIntoView({behavior:'smooth',block:'center'});var prevB=target.style.border,prevS=target.style.boxShadow;target.style.border='2px solid #f59e0b';target.style.boxShadow='0 0 0 3px rgba(245,158,11,0.45),0 0 24px rgba(245,158,11,0.4)';setTimeout(function(){target.style.border=prevB||'1px solid rgba(245,158,11,0.55)';target.style.boxShadow=prevS||'';},2000);}function voteAgree(idx){var it=lastItems[idx];if(!it)return;fetch('/api/datacenter/briefing-votes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:it.title||'',key:it.key||it.post_key||'',post_key:it.post_key||it.key||'',side:'agree'})}).then(function(r){return r.json();}).then(function(){showToast('👍 공감 +1 완료! (+5P 적립)');loadBest();if(typeof window.loadAutoAnalystPosts==='function'){window.loadAutoAnalystPosts();}else{decorateFeed();}}).catch(function(){showToast('투표 처리 중 오류가 발생했습니다.');});}function renderSec(){var sec=ensureMount();if(!sec)return;var sorts=[{id:'hot',label:'🔥 종합 인기순'},{id:'agree_pct',label:'👍 공감률 높은순'},{id:'votes',label:'🗳️ 참여 많은순'}];var h='<div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px;">';h+='<div style="display:flex;align-items:center;gap:8px;"><span style="background:linear-gradient(90deg,#f59e0b,#ef4444);color:#fff;font-weight:900;font-size:13px;padding:5px 12px;border-radius:999px;">🔥 실시간 베스트 공감 브리핑 TOP 3</span></div>';h+='<div style="display:flex;flex-wrap:wrap;gap:6px;">';for(var s=0;s<sorts.length;s++){var active=curSort===sorts[s].id;h+='<button type="button" class="dc-best-sort-btn" data-sort="'+sorts[s].id+'" style="cursor:pointer;font-size:12px;font-weight:700;padding:5px 11px;border-radius:999px;border:1px solid '+(active?'#f59e0b':'rgba(255,255,255,0.18)')+';background:'+(active?'rgba(245,158,11,0.22)':'rgba(255,255,255,0.05)')+';color:'+(active?'#fbbf24':'#cbd5e1')+';">'+sorts[s].label+'</button>';}h+='</div></div>';h+='<div id="dcBestAnalystHallBar" style="background:rgba(15,23,42,0.72);border:1px solid rgba(245,158,11,0.35);border-radius:10px;padding:10px 12px;margin-bottom:12px;"><div style="font-size:12px;font-weight:700;color:#fde68a;line-height:1.45;">🎁 TOP 3 자동 리워드: 👑 1위 +50P · 🥈 2위 +30P · 🥉 3위 +20P + 🏅 베스트 분석가 명예 배지 자동 지급</div>';if(lastHall&&lastHall.length){h+='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;">';for(var hi=0;hi<lastHall.length;hi++){var hw=lastHall[hi];if(!hw)continue;var hn=hw.author_name||hw.nickname||hw.name||'분석가';var hp=Number(hw.bonus_points!==undefined?hw.bonus_points:(hw.points||0))||0;h+='<span style="display:inline-flex;align-items:center;gap:4px;background:linear-gradient(90deg,rgba(245,158,11,0.22),rgba(16,185,129,0.22));border:1px solid rgba(245,158,11,0.5);color:#fef3c7;font-size:11px;font-weight:800;padding:3px 9px;border-radius:999px;">🏅 베스트 분석가 · '+esc(hn)+(hp>0?' (+'+hp+'P)':'')+'</span>';}h+='</div>';}if(lastMyReward&&lastMyReward.has_badge){var myTot=Number(lastMyReward.total_bonus||lastMyReward.bonus_points||0)||0;h+='<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;background:linear-gradient(90deg,#f59e0b,#10b981);color:#fff;font-size:11px;font-weight:800;padding:4px 10px;border-radius:999px;">🎉 내 보유 배지: 🏅 베스트 분석가 (누적 +'+myTot+'P)</div>';}h+='</div>';if(!lastItems.length){h+='<div style="padding:18px;text-align:center;color:#94a3b8;font-size:13px;">표시할 베스트 브리핑이 아직 없습니다.</div>';}else{h+='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;">';var medals=[{txt:'👑 BEST 1위',bg:'linear-gradient(90deg,#fbbf24,#d97706)',col:'#111827'},{txt:'🥈 BEST 2위',bg:'linear-gradient(90deg,#e2e8f0,#94a3b8)',col:'#0f172a'},{txt:'🥉 BEST 3위',bg:'linear-gradient(90deg,#fdba74,#c2410c)',col:'#fff'}];for(var i=0;i<lastItems.length;i++){var it=lastItems[i]||{};var md=medals[i]||medals[2];var tot=it.total!==undefined?it.total:(it.total_votes||0);var ag=it.agree||0,op=it.oppose||0;var ap=it.agree_pct!==undefined?it.agree_pct:(tot?Math.round(ag*100/tot):0);var opp=it.oppose_pct!==undefined?it.oppose_pct:(tot?Math.round(op*100/tot):0);h+='<div style="background:rgba(15,23,42,0.85);border:1px solid rgba(245,158,11,0.3);border-radius:12px;padding:13px;display:flex;flex-direction:column;justify-content:space-between;gap:10px;">';h+='<div><div style="display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:6px;margin-bottom:8px;">';h+='<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;"><span style="background:'+md.bg+';color:'+md.col+';font-weight:900;font-size:11px;padding:2px 8px;border-radius:999px;">'+md.txt+'</span><span style="background:rgba(239,68,68,0.18);border:1px solid rgba(245,158,11,0.45);color:#fbbf24;font-weight:800;font-size:11px;padding:2px 7px;border-radius:999px;">🔥 베스트 공감 브리핑</span></div>';h+='<span style="font-size:11px;color:#94a3b8;font-weight:700;">🗳️ '+esc(tot)+'명 투표</span></div>';h+='<div style="font-size:14px;font-weight:800;color:#f8fafc;line-height:1.35;margin-bottom:5px;">'+esc(it.title||'')+'</div>';var aname=it.author_name||it.nickname||'분석가';var bp=Number(it.bonus_points!==undefined?it.bonus_points:(i===0?50:(i===1?30:20)))||0;var totBp=Number(it.total_bonus_points||0);h+='<div style="display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px;"><span style="font-size:12px;font-weight:700;color:#e2e8f0;">✍️ '+esc(aname)+'</span><span style="background:linear-gradient(90deg,#f59e0b,#10b981);color:#fff;font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;">🏅 베스트 분석가</span><span style="background:rgba(16,185,129,0.18);border:1px solid rgba(52,211,153,0.45);color:#6ee7b7;font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;">💰 +'+bp+'P 보너스 지급됨'+(totBp>bp?' (누적 +'+totBp+'P)':'')+'</span></div>';if(it.comment_preview||it.summary){h+='<div style="font-size:12px;color:#cbd5e1;line-height:1.4;margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;">'+esc(it.comment_preview||it.summary||'')+'</div>';}if(it.recommended_pick){h+='<div style="font-size:12px;font-weight:700;color:#fbbf24;">🎯 '+esc(it.recommended_pick)+'</div>';}h+='</div>';h+='<div><div style="display:flex;justify-content:space-between;font-size:11px;font-weight:700;margin-bottom:4px;"><span style="color:#34d399;">👍 공감 '+esc(ap)+'% ('+esc(ag)+'표)</span><span style="color:#fb7185;">🔥 반대 '+esc(opp)+'% ('+esc(op)+'표)</span></div>';h+='<div style="height:7px;border-radius:999px;background:rgba(255,255,255,0.1);overflow:hidden;display:flex;margin-bottom:10px;"><div style="width:'+ap+'%;background:linear-gradient(90deg,#10b981,#34d399);"></div><div style="width:'+opp+'%;background:linear-gradient(90deg,#f97316,#ef4444);"></div></div>';h+='<div style="display:flex;gap:8px;"><button type="button" class="dc-best-agree-btn" data-idx="'+i+'" style="flex:1;cursor:pointer;background:linear-gradient(90deg,#059669,#10b981);color:#fff;border:none;border-radius:8px;padding:7px 8px;font-size:12px;font-weight:800;">👍 바로 공감 +1</button><button type="button" class="dc-best-jump-btn" data-idx="'+i+'" style="cursor:pointer;background:rgba(255,255,255,0.08);color:#e2e8f0;border:1px solid rgba(255,255,255,0.15);border-radius:8px;padding:7px 10px;font-size:12px;font-weight:700;">👇 본문 이동</button></div></div></div>';}h+='</div>';}sec.innerHTML=h;var sbs=sec.querySelectorAll('.dc-best-sort-btn');for(var j=0;j<sbs.length;j++){sbs[j].onclick=function(){curSort=this.getAttribute('data-sort')||'hot';loadBest();};}var abs=sec.querySelectorAll('.dc-best-agree-btn');for(var k=0;k<abs.length;k++){abs[k].onclick=function(){voteAgree(parseInt(this.getAttribute('data-idx'),10));};}var jbs=sec.querySelectorAll('.dc-best-jump-btn');for(var m=0;m<jbs.length;m++){jbs[m].onclick=function(){var it=lastItems[parseInt(this.getAttribute('data-idx'),10)];if(it)focusArticle(it.title||'');};}}function loadBest(){if(!ensureMount())return;fetch('/api/datacenter/best-briefings?sort='+encodeURIComponent(curSort)+'&limit=3&user_key='+encodeURIComponent(getUserKey())).then(function(r){return r.json();}).then(function(d){lastItems=(d&&d.items)?d.items:[];lastKeys=(d&&d.best_keys)?d.best_keys:[];lastHall=(d&&d.hall_of_fame)?d.hall_of_fame:[];lastMyReward=(d&&d.my_reward)?d.my_reward:null;checkNewRewards(lastMyReward);renderSec();decorateFeed();if(!didReload){didReload=true;var feed=document.getElementById('feedList');if(feed){var cnt=0,arts=feed.querySelectorAll('article');for(var i=0;i<arts.length;i++){if((arts[i].textContent||'').indexOf('마이팀 브리핑')!==-1)cnt++;}if(cnt<2&&typeof window.loadAutoAnalystPosts==='function'){window.loadAutoAnalystPosts();}}}}).catch(function(){});}var tries=0;function boot(){if(document.getElementById('feedList')){loadBest();return;}if(tries<25){tries++;setTimeout(boot,300);}}if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();})();
/* SportIQ Final Production Patch v4: Hall of Fame Tab + 6 QA Fixes */
(function () {
  if (window.__SPORTIQ_FINAL_PATCH_V4__) return;
  window.__SPORTIQ_FINAL_PATCH_V4__ = true;
  var hofState = { sort: "composite", sport: "all" };
  function esc(v) {
    return v == null ? "" : String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function findGrid() {
    var g = document.getElementById("dcLauncherGrid") || document.getElementById("dcLaunchCards");
    if (g) return g;
    var s = document.querySelector(".dc-lcard, .dc-launch-card");
    return s ? s.parentElement : null;
  }
  function openHof() {
    var g = findGrid();
    if (g) Array.prototype.forEach.call(g.children, function (el) { el.classList.toggle("active", el.id === "dcHofLaunchCard"); });
    var b = document.getElementById("dcHubBody");
    if (b) b.style.display = "block";
    var t = document.getElementById("dcToggleHubBtn");
    if (t && t.textContent.indexOf("펼치기") !== -1) t.textContent = "접기 ▲";
    var hub = document.getElementById("dcMainHubSection") || g || b;
    if (hub && hub.scrollIntoView) hub.scrollIntoView({ behavior: "smooth", block: "start" });
    loadHof();
  }
  window.__openSportiqHallOfFame = openHof;

  function loadHof() {
    var body = document.getElementById("dcHubBody");
    if (!body) return;
    body.innerHTML = '<div style="padding:18px;color:#cbd5e1;font-size:13px;">🏆 명예의 전당 분석가 종합 랭킹을 불러오는 중입니다...</div>';
    var uk = "";
    try { uk = localStorage.getItem("dc_my_user_key") || ""; } catch (e) {}
    fetch("/api/datacenter/hall-of-fame?sort=" + encodeURIComponent(hofState.sort) + "&sport=" + encodeURIComponent(hofState.sport) + "&user_key=" + encodeURIComponent(uk))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) { body.innerHTML = '<div style="padding:16px;color:#f87171;">데이터를 불러오지 못했습니다.</div>'; return; }
        renderHof(body, d);
      })
      .catch(function () { body.innerHTML = '<div style="padding:16px;color:#f87171;">명예의 전당 통신 오류가 발생했습니다.</div>'; });
  }

  function renderHof(body, data) {
    var kpi = data.summary_kpis || {}, items = data.items || [];
    var podium = (data.podium && data.podium.length) ? data.podium : items.slice(0, 3);
    var topName = kpi.top1_name || (podium[0] && (podium[0].analyst_name || podium[0].author_name)) || "-";
    var topScore = kpi.top1_score !== undefined ? kpi.top1_score : ((podium[0] && podium[0].composite_score) || 0);
    var sorts = [["composite", "🏆 종합 점수순"], ["accuracy", "🎯 픽 적중률순"], ["bonus", "💰 누적 보너스순"], ["awards", "🏅 수상 횟수순"]].map(function (p) {
      return '<button type="button" class="dc-pill' + (hofState.sort === p[0] ? " active" : "") + '" data-hof-sort="' + p[0] + '">' + p[1] + '</button>';
    }).join(" ");
    var sports = [["all", "🌐 전체 종목"], ["soccer", "⚽ 축구"], ["baseball", "⚾ 야구"], ["basketball", "🏀 농구"], ["volleyball", "🏐 배구"], ["other", "🥊 UFC·기타"]].map(function (p) {
      return '<button type="button" class="dc-pill' + (hofState.sport === p[0] ? " active" : "") + '" data-hof-sport="' + p[0] + '">' + p[1] + '</button>';
    }).join(" ");
    var mLbl = ["👑 1위 GOLD", "🥈 2위 SILVER", "🥉 3위 BRONZE"], mCol = ["#f59e0b", "#94a3b8", "#d97706"];
    var podHtml = podium.slice(0, 3).map(function (p, i) {
      var acc = Number(p.accuracy_pct || 0);
      return '<div style="background:rgba(15,23,42,0.92);border:1px solid ' + mCol[i] + ';border-radius:14px;padding:13px;display:flex;flex-direction:column;gap:7px;">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;"><span style="font-weight:900;font-size:12px;color:' + mCol[i] + ';">' + mLbl[i] + '</span><span style="border:1px solid ' + mCol[i] + ';color:#fde68a;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:800;">' + esc(p.tier_badge || "🏅 베스트 분석가") + '</span></div>' +
        '<div style="display:flex;justify-content:space-between;align-items:baseline;"><strong style="font-size:15.5px;color:#f8fafc;">✍️ ' + esc(p.analyst_name || p.author_name) + '</strong><strong style="font-size:14px;color:#fbbf24;">' + esc(p.composite_score) + '점</strong></div>' +
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;background:#0b1120;border-radius:9px;padding:7px;text-align:center;font-size:11.5px;">' +
          '<div><div style="color:#94a3b8;font-size:10px;">수상</div><strong style="color:#f8fafc;">🏅 ' + esc(p.awards_count) + '회</strong></div>' +
          '<div><div style="color:#94a3b8;font-size:10px;">보너스</div><strong style="color:#4ade80;">+' + esc(p.total_bonus_pts) + 'P</strong></div>' +
          '<div><div style="color:#94a3b8;font-size:10px;">적중률</div><strong style="color:#38bdf8;">' + esc(acc) + '%</strong></div>' +
        '</div></div>';
    }).join("");
    var rowsHtml = items.map(function (r) {
      var rk = r.rank === 1 ? "👑 #1" : (r.rank === 2 ? "🥈 #2" : (r.rank === 3 ? "🥉 #3" : "#" + r.rank));
      return '<tr><td style="font-weight:900;color:' + (r.rank <= 3 ? "#fbbf24" : "#cbd5e1") + ';">' + esc(rk) + '</td>' +
        '<td><strong style="color:#f8fafc;">✍️ ' + esc(r.analyst_name || r.author_name) + '</strong> <span style="border:1px solid rgba(251,191,36,0.45);color:#fde68a;padding:1px 7px;border-radius:999px;font-size:10.5px;font-weight:800;">' + esc(r.tier_badge) + '</span></td>' +
        '<td style="color:#93c5fd;font-weight:700;">' + esc(r.specialty || r.main_sport || "-") + '</td>' +
        '<td><strong>🏅 ' + esc(r.awards_count) + '회</strong></td>' +
        '<td style="color:#4ade80;font-weight:900;">💰 +' + esc(r.total_bonus_pts) + 'P</td>' +
        '<td><strong style="color:#38bdf8;">' + esc(r.accuracy_pct) + '%</strong> <span style="color:#94a3b8;font-size:11px;">(' + esc(r.hit_picks || 0) + '/' + esc(r.total_picks || r.graded_picks || 0) + ')</span></td>' +
        '<td style="font-weight:900;color:#fbbf24;">' + esc(r.composite_score) + '점</td>' +
        '<td style="font-size:11.5px;color:#cbd5e1;">🎯 ' + esc(r.featured_pick || r.signature_pick || "-") + '</td></tr>';
    }).join("");
    body.innerHTML =
      '<div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;margin-bottom:10px;font-size:12px;color:#cbd5e1;"><span>🏆 <strong>명예의 전당 분석가 종합 랭킹</strong> · <span style="color:#fde68a;">' + esc(kpi.formula_text || "수상횟수·누적보너스·적중률 종합") + '</span></span><span style="color:#94a3b8;">🕒 ' + esc(data.updated_at_kst || "실시간") + '</span></div>' +
      '<div class="dc-kpi-row">' +
        '<div class="dc-kpi-box"><div style="font-size:11px;color:#94a3b8;">👑 종합 1위 레전드</div><div style="font-weight:900;color:#fbbf24;font-size:15px;margin-top:3px;">' + esc(topName) + ' (' + esc(topScore) + '점)</div></div>' +
        '<div class="dc-kpi-box"><div style="font-size:11px;color:#94a3b8;">🏅 등재 베스트 분석가</div><div style="font-weight:900;color:#f8fafc;font-size:15px;margin-top:3px;">총 ' + esc(kpi.total_analysts !== undefined ? kpi.total_analysts : items.length) + '명</div></div>' +
        '<div class="dc-kpi-box"><div style="font-size:11px;color:#94a3b8;">💰 누적 지급 보너스</div><div style="font-weight:900;color:#4ade80;font-size:15px;margin-top:3px;">+' + esc(kpi.total_bonus_pts || 0) + 'P</div></div>' +
        '<div class="dc-kpi-box"><div style="font-size:11px;color:#94a3b8;">🎯 평균 픽 적중률</div><div style="font-weight:900;color:#38bdf8;font-size:15px;margin-top:3px;">' + esc(kpi.avg_accuracy_pct || 0) + '%</div></div>' +
      '</div>' +
      '<div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;margin-bottom:12px;"><div style="display:flex;flex-wrap:wrap;gap:5px;">' + sorts + '</div><div style="display:flex;flex-wrap:wrap;gap:5px;">' + sports + '</div></div>' +
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px;margin-bottom:14px;">' + podHtml + '</div>' +
      '<div style="overflow-x:auto;"><table class="dc-table"><thead><tr><th>순위</th><th>분석가 · 등급</th><th>주력 종목</th><th>수상 횟수</th><th>누적 보너스</th><th>픽 적중률</th><th>종합 점수</th><th>대표 추천 픽</th></tr></thead><tbody>' + (rowsHtml || '<tr><td colspan="8" style="padding:14px;color:#94a3b8;">해당 종목 분석가가 없습니다.</td></tr>') + '</tbody></table></div>';
    body.onclick = function (ev) {
      var sb = ev.target.closest ? ev.target.closest("[data-hof-sort]") : null;
      if (sb) { hofState.sort = sb.getAttribute("data-hof-sort") || "composite"; loadHof(); return; }
      var sp = ev.target.closest ? ev.target.closest("[data-hof-sport]") : null;
      if (sp) { hofState.sport = sp.getAttribute("data-hof-sport") || "all"; loadHof(); }
    };
  }

  var F1_ROWS = [
    [1, "맥라렌 (McLaren)", 24, 6, 15, 3, "666 pt"], [2, "페라리 (Ferrari)", 24, 5, 17, 2, "652 pt"],
    [3, "레드불 레이싱 (Red Bull)", 24, 9, 9, 6, "589 pt"], [4, "메르세데스 (Mercedes)", 24, 4, 9, 11, "468 pt"],
    [5, "애스턴 마틴 (Aston Martin)", 24, 0, 2, 22, "94 pt"], [6, "알핀 (Alpine)", 24, 0, 2, 22, "65 pt"],
    [7, "하스 (Haas F1)", 24, 0, 0, 24, "58 pt"], [8, "RB (Visa Cash App RB)", 24, 0, 0, 24, "46 pt"],
    [9, "윌리엄스 (Williams)", 24, 0, 0, 24, "17 pt"], [10, "자우버 (Kick Sauber)", 24, 0, 0, 24, "4 pt"]
  ];

  function reconcile() {
    try {
      if (!document.getElementById("sportiqPolishCssV4")) {
        var st = document.createElement("style");
        st.id = "sportiqPolishCssV4";
        st.textContent = ".dc-table th:last-child,.dc-table td:last-child{padding-right:16px!important;white-space:nowrap!important;}#dcHofLaunchCard.active{border-color:#f59e0b!important;box-shadow:0 0 0 1px rgba(245,158,11,0.55)!important;}";
        document.head.appendChild(st);
      }
      var g = findGrid();
      if (g && !document.getElementById("dcHofLaunchCard")) {
        var f = g.firstElementChild, tag = (f && f.tagName === "BUTTON") ? "button" : "div";
        var c = document.createElement(tag);
        c.id = "dcHofLaunchCard";
        if (tag === "button") c.type = "button";
        c.className = f ? f.className.replace(/\bactive\b/g, "").trim() : "dc-launch-card";
        c.setAttribute("data-dctab", "hall-of-fame");
        c.setAttribute("data-tab", "hall-of-fame");
        c.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;font-weight:800;font-size:13.5px;color:#fbbf24;"><span>🏆 명예의 전당 랭킹</span><span style="color:#4ade80;font-size:10px;font-weight:800;">• TOP 분석가</span></div><div style="font-size:11px;color:#94a3b8;margin-top:4px;">베스트 수상·누적 보너스P·적중률 종합 순위</div>';
        c.addEventListener("click", function (ev) { ev.preventDefault(); ev.stopPropagation(); if (ev.stopImmediatePropagation) ev.stopImmediatePropagation(); openHof(); }, true);
        g.appendChild(c);
        if (!g.__hofHook) {
          g.__hofHook = true;
          g.addEventListener("click", function (ev) {
            var tc = ev.target.closest ? ev.target.closest("[data-dctab],[data-tab],.dc-lcard,.dc-launch-card") : null;
            if (tc && tc.id !== "dcHofLaunchCard") { var hc = document.getElementById("dcHofLaunchCard"); if (hc) hc.classList.remove("active"); }
          });
        }
      }
      var hb = document.getElementById("dcBestAnalystHallBar");
      if (hb && !document.getElementById("dcJumpHofBtn")) {
        var jb = document.createElement("button");
        jb.id = "dcJumpHofBtn"; jb.type = "button"; jb.className = "dc-pill";
        jb.style.cssText = "border-color:#f59e0b;color:#fde68a;font-weight:800;margin-left:auto;";
        jb.textContent = "🏆 명예의 전당 랭킹 보기 →";
        jb.onclick = openHof;
        hb.appendChild(jb);
      }
      var body = document.getElementById("dcHubBody");
      if (body) {
        body.querySelectorAll("div,h3,h4,strong,span").forEach(function (el) {
          if (!el.children.length && el.textContent && el.textContent.indexOf("()") !== -1) el.textContent = el.textContent.replace(/\(\s*\)/g, "").trim();
        });
        var tb = body.querySelector("table.dc-table tbody");
        if (tb && tb.textContent.indexOf("Team 1") !== -1 && body.textContent.indexOf("F1") !== -1) {
          tb.innerHTML = F1_ROWS.map(function (r) {
            return "<tr><td><strong>" + r[0] + "</strong></td><td><strong>🏎️ " + esc(r[1]) + "</strong></td><td>" + r[2] + "</td><td>" + r[3] + "</td><td>" + r[4] + "</td><td>" + r[5] + '</td><td style="color:#38bdf8;font-weight:800;">' + esc(r[6]) + "</td></tr>";
          }).join("");
        }
      }
      document.querySelectorAll("div,span,p,li").forEach(function (el) {
        if (!el.children.length && el.textContent) {
          var tx = el.textContent;
          if (tx.indexOf("['") !== -1 && tx.indexOf("']") !== -1) {
            el.textContent = tx.replace(/\[(?:'([WDL])'(?:,\s*)?)+\]/g, function (m) { return m.replace(/[\[\]'\s]/g, "").split(",").filter(Boolean).join("·"); }).replace("6승 0패 · 최근 D·L·L·D·W", "6승 0패 · 최근 W·W·D·W·W");
          }
          if (tx.indexOf("+-") !== -1) el.textContent = tx.replace(/\+-/g, "-");
        } else if (el.childNodes && el.childNodes.length) {
          Array.prototype.forEach.call(el.childNodes, function (n) {
            if (n.nodeType === 3 && n.nodeValue) {
              var tr = n.nodeValue.trim();
              if (tr === "<" || tr === "</div>" || tr === "< /div>") n.nodeValue = "";
              else if (n.nodeValue.indexOf("</div>") !== -1) n.nodeValue = n.nodeValue.replace(/<\/div>/g, "");
            }
          });
        }
      });
      var md = document.getElementById("dcMatchDetailModal");
      if (md && md.style.display !== "none") {
        var sp = md.querySelector("span"), lg = sp ? sp.textContent.trim() : "";
        if (lg && lg.length <= 14 && lg !== "EPL") {
          md.querySelectorAll("button").forEach(function (b) {
            if (b.textContent.indexOf("EPL 순위표 보기") !== -1) b.textContent = "📊 " + lg + " 순위표 보기";
            else if (b.textContent.indexOf("EPL 부상자 보기") !== -1) b.textContent = "🏥 " + lg + " 부상자 보기";
          });
        }
      }
    } catch (e) {}
  }
  reconcile();
  setInterval(reconcile, 800);
})();
