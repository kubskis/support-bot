(function(){
  var root=document.getElementById('adm'); if(!root) return;
  function el(tag,cls,text){var e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e}
  function link(cls,text,href){var a=el('a',cls,text); a.href=href; return a}
  function btn(text,cls,fn){var b=el('button',cls||'btn alt',text); b.type='button'; if(fn) b.addEventListener('click',fn); return b}
  function card(title){var c=el('section','cab-card'); if(title) c.appendChild(el('h2',null,title)); return c}
  function note(t,c){return el('p',c||'cab-note',t)}
  function clear(n){while(n.firstChild) n.removeChild(n.firstChild)}
  function api(path,body){
    var o={cache:'no-store',credentials:'same-origin'};
    if(body!==undefined){o.method='POST'; o.headers={'Content-Type':'application/json','X-Requested-With':'tos'}; o.body=JSON.stringify(body)}
    return fetch(path,o).then(function(r){return r.json().catch(function(){return {}}).then(function(d){d._status=r.status; return d})});
  }
  function dt(ts){ if(!ts) return '—'; try{return new Date(ts*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'})}catch(e){return ''} }
  function dur(sec){ if(sec===null||sec===undefined) return '—'; var m=Math.round(sec); if(m<60) return m+' мин'; var h=Math.floor(m/60); return h<48? h+' ч '+(m%60)+' мин' : Math.round(h/24)+' дн' }
  var ST={pending:'ждёт ответа',active:'в работе',closed:'закрыто',rejected:'отклонено'};
  var ERR={auth:'Нужно войти.',forbidden:'Нет доступа.',csrf:'Запрос отклонён. Обновите страницу.',rate:'Слишком часто. Подождите минуту.',db:'База не отвечает.',not_active:'Отвечать и закрывать можно только обращения «в работе».',send_failed:'Telegram не принял сообщение (игрок мог заблокировать бота).',not_found:'Не найдено.',bad_request:'Некорректный запрос.'};
  function errText(d){return ERR[d&&d.error]||'Не удалось выполнить. Попробуйте ещё раз.'}

  var state={tab:'overview',tickets:{status:'',cat:'',admin:'',q:''},helperDays:'30',me:null};
  var pane=null;

  // ---------- каркас ----------
  function viewDenied(code){
    clear(root);
    var c=card(code===401?'Нужно войти':'Раздел только для главных админов');
    c.appendChild(note(code===401?'Войдите в личный кабинет через Telegram, затем вернитесь сюда.':'У вашего аккаунта нет доступа к админке. Права проверяет сервер при каждом действии.'));
    c.appendChild(link('btn','Перейти в кабинет','/cabinet')); root.appendChild(c);
  }
  var TABS=[['overview','Обзор'],['tickets','Обращения'],['control','Управление'],['channel','Канал']];
  function shell(){
    clear(root);
    var back=link('btn alt adm-back','← В кабинет','/cabinet'); back.setAttribute('aria-label','Вернуться в личный кабинет');
    var bar=el('div','chips adm-tabs'); bar.setAttribute('role','tablist'); bar.appendChild(back);
    TABS.forEach(function(t){var b=btn(t[1],null,function(){go(t[0])}); b.setAttribute('role','tab'); b.setAttribute('aria-pressed',state.tab===t[0]?'true':'false'); bar.appendChild(b)});
    root.appendChild(bar);
    pane=el('div'); root.appendChild(pane);
  }
  function go(tab){state.tab=tab; try{history.replaceState(null,'','#'+tab)}catch(e){} shell(); render()}
  function render(){
    pane.appendChild(note('Загружаю…'));
    ({overview:tabOverview,tickets:tabTickets,control:tabControl,channel:tabChannel})[state.tab]();
  }
  function fail(d){
    if(d&&(d._status===401||d._status===403)){viewDenied(d._status); return true}
    return false;
  }
  function tiles(list){var g=el('div','stats'); list.forEach(function(x){var s=el('div','stat'); s.appendChild(el('b',null,String(x[0]))); s.appendChild(el('span',null,x[1])); g.appendChild(s)}); return g}

  // ---------- обзор ----------
  function cacheGet(k){try{var v=JSON.parse(sessionStorage.getItem('adm:'+k)||'null'); return v&&Date.now()-v.t<600000?v.d:null}catch(e){return null}}
  function cacheSet(k,d){try{sessionStorage.setItem('adm:'+k,JSON.stringify({t:Date.now(),d:d}))}catch(e){}}
  function tabOverview(){
    var cached=cacheGet('overview'), paintedFor=null;
    if(cached){paintOverview(cached); paintedFor=JSON.stringify(cached)}
    api('/api/admin/overview').then(function(d){
      if(fail(d)) return;
      if(!d.error){cacheSet('overview',d); if(JSON.stringify(d)===paintedFor) return}
      if(state.tab!=='overview') return;
      paintOverview(d);
    }).catch(function(){if(!cached){clear(pane); pane.appendChild(note('Не удалось загрузить. Обновите страницу.'))}});
  }
  function paintOverview(d){
    {
      clear(pane);
      if(d.error){pane.appendChild(note(errText(d))); return}
      state.me=d.me;
      var t=d.tickets||{};
      var c=card('Сейчас');
      c.appendChild(tiles([[t.pending||0,'ждут ответа'+(t.pending&&t.oldest_wait_h?' (старое '+t.oldest_wait_h+' ч)':'')],[t.active||0,'в работе'],[t.closed||0,'закрыто всего'],[t.rejected||0,'отклонено всего'],[d.users||0,'пользователей'],[t.avg?t.avg+' / 5':'—','оценка поддержки']]));
      var f=d.flags||{}, ul=el('ul','tlist');
      function row(a,b){var li=el('li'); li.appendChild(el('span','tc',a)); li.appendChild(el('b',null,b)); ul.appendChild(li)}
      row('Публикации в канал',f.paused?'⏸ остановлены':'▶️ идут');
      row('Набор в хелперы',f.helpers_open?'открыт':'закрыт'); row('Набор в искатели',f.seekers_open?'открыт':'закрыт');
      row('Технический перерыв',f.maintenance?'включён':'выключен'); row('Тест-режим',f.test_mode?'включён':'выключен');
      row('Праздничный режим',f.event||'выключен');
      if(f.toh&&f.toh.playing!=null) row('Сейчас играют в Tower of Hell',String(f.toh.playing));
      c.appendChild(ul); pane.appendChild(c);

      var ch=card('Обращения за 14 дней');
      var days=d.days||[], mx=1; days.forEach(function(x){mx=Math.max(mx,x.new,x.closed)});
      var wrap=el('div','adm-chart');
      days.forEach(function(x){
        var col=el('div','adm-col'); col.title=x.d+': новых '+x.new+', закрыто '+x.closed;
        var a=el('i','n'); a.style.height=Math.round(x.new*100/mx)+'%'; var b=el('i','d'); b.style.height=Math.round(x.closed*100/mx)+'%';
        col.appendChild(a); col.appendChild(b); col.appendChild(el('span',null,x.d.slice(8))); wrap.appendChild(col);
      });
      ch.appendChild(wrap);
      var lg=note('','cab-note small'); lg.appendChild(el('span','lg lg-new','новые')); lg.appendChild(document.createTextNode(' ')); lg.appendChild(el('span','lg lg-done','закрытые')); ch.appendChild(lg);
      pane.appendChild(ch);

      var tm=card('Рейтинг команды');
      if(d.team&&d.team.length){
        var u=el('ul','tlist trank');
        d.team.forEach(function(x,i){var li=el('li'); li.appendChild(el('span','medal',['🥇','🥈','🥉'][i]||(i+1)+'.')); li.appendChild(el('span','tc',x.name+(x.agent?' · агент #'+x.agent:''))); li.appendChild(el('b',null,x.rated?x.avg+' / 5':'—')); li.appendChild(el('span','tm','закрыто '+x.closed+', оценок '+x.rated)); u.appendChild(li)});
        tm.appendChild(u);
      } else tm.appendChild(note('Пока нет закрытых обращений.'));
      tm.appendChild(btn('Все обращения →',null,function(){go('tickets')}));
      pane.appendChild(tm);
    }
  }

  // ---------- обращения ----------
  var tkBox, tkMore, tkLast=0;
  function tabTickets(){
    clear(pane);
    var f=state.tickets, c=card('Обращения');
    var chips=el('div','chips');
    [['','Все'],['open','Открытые'],['closed','Закрытые'],['rejected','Отклонённые']].forEach(function(s){
      var b=btn(s[1],null,function(){f.status=s[0]; tabTickets()}); b.setAttribute('aria-pressed',f.status===s[0]?'true':'false'); chips.appendChild(b)});
    c.appendChild(chips);
    var form=el('div','adm-form');
    var q=el('input'); q.type='search'; q.placeholder='№ обращения, ID, @ник или текст'; q.value=f.q; q.maxLength=80; q.setAttribute('aria-label','Поиск');
    var cat=el('select'); cat.setAttribute('aria-label','Тип');
    [['','Все типы'],['complaint','Жалобы'],['appeal','Обжалования'],['question','Вопросы'],['friends','Друзья']].forEach(function(o){var op=el('option',null,o[1]); op.value=o[0]; cat.appendChild(op)}); cat.value=f.cat;
    var go_=btn('Найти','btn',function(){f.q=q.value.trim(); f.cat=cat.value; tabTickets()});
    q.addEventListener('keydown',function(e){if(e.key==='Enter') go_.click()});
    var who=el('select'); who.setAttribute('aria-label','Хелпер'); var o0=el('option',null,'Все хелперы'); o0.value=''; who.appendChild(o0);
    function fillWho(items){items.forEach(function(h){var op=el('option',null,h.name); op.value=String(h.id); who.appendChild(op)}); who.value=f.admin||''}
    var hc=cacheGet('helpers');
    if(hc) fillWho(hc); else api('/api/admin/helpers?days=all').then(function(d){if(d&&d.items){var l=d.items.map(function(h){return {id:h.id,name:h.name}}); cacheSet('helpers',l); fillWho(l)}});
    who.addEventListener('change',function(){f.admin=who.value; f.q=q.value.trim(); f.cat=cat.value; tabTickets()});
    form.appendChild(q); form.appendChild(cat); form.appendChild(who); form.appendChild(go_); c.appendChild(form);
    tkBox=el('ul','tlist adm-tk'); c.appendChild(tkBox);
    tkMore=btn('Показать ещё',null,function(){loadTickets(false)}); tkMore.hidden=true; c.appendChild(tkMore);
    pane.appendChild(c); tkLast=0; loadTickets(true);
  }
  function loadTickets(first){
    var f=state.tickets, qs=[];
    if(f.status) qs.push('status='+encodeURIComponent(f.status)); if(f.cat) qs.push('cat='+f.cat); if(f.admin) qs.push('admin='+f.admin);
    if(f.q) qs.push('q='+encodeURIComponent(f.q)); if(tkLast) qs.push('before='+tkLast);
    if(first) tkBox.appendChild(el('li','tm','Загружаю…'));
    api('/api/admin/tickets?'+qs.join('&')).then(function(d){
      if(fail(d)) return; if(first) clear(tkBox);
      if(d.error){tkBox.appendChild(el('li','tm',errText(d))); return}
      if(first&&!d.items.length) tkBox.appendChild(el('li','tm','Ничего не найдено.'));
      d.items.forEach(function(t){
        var li=el('li','tk'); var a=el('button','tk-b'); a.type='button';
        a.appendChild(el('b',null,'№'+t.id)); a.appendChild(el('span','pill '+(t.status==='closed'?'ok':'off'),ST[t.status]||t.status));
        a.appendChild(el('span','tc',t.cat+(t.uname?' · @'+t.uname:' · ID '+(t.user||'?'))));
        var ch=el('span','chain'); chainInto(ch,t.chain,t.status); a.appendChild(ch);
        a.appendChild(el('span','tm',(t.score?'⭐'+t.score+' · ':'')+(t.msgs?t.msgs+' сообщ. · ':'')+dt(t.updated)));
        a.addEventListener('click',function(){openTicket(t.id)}); li.appendChild(a); tkBox.appendChild(li); tkLast=t.id;
      });
      tkMore.hidden=!d.more;
    });
  }
  function chainInto(box,chain,status){
    if(!chain||!chain.length){box.appendChild(el('span','tm',status==='pending'?'Пока никто не взял':'Хелпер неизвестен')); return}
    box.appendChild(el('span','tm',chain.length>1?'Передавали: ':'Вёл: '));
    chain.forEach(function(h,i){
      if(i){var ar=el('span','arrow','→'); ar.setAttribute('aria-label','передал'); box.appendChild(ar)}
      box.appendChild(el('b',i===chain.length-1&&chain.length>1?'h last':'h',h.name));
    });
  }
  function bubble(m){
    var b=el('div','msg '+m.kind);
    if(m.kind==='system'){b.textContent=m.text+(m.who?' · '+m.who:'')+' · '+dt(m.at); return b}
    var h=el('div','mh',(m.kind==='admin'?(m.who||'Хелпер'):'Игрок')+' · '+dt(m.at)+(m.via==='site'?' · с сайта':'')+(m.media?' · '+(m.media==='photo'?'📷 фото':'📎 вложение'):''));
    b.appendChild(h); b.appendChild(el('div','mt',m.text||'(без текста)')); return b;
  }
  function openTicket(id){
    clear(pane); var c=card('Обращение №'+id); pane.appendChild(c);
    c.appendChild(btn('← К списку',null,function(){tabTickets()}));
    var body=el('div'); c.appendChild(body); body.appendChild(note('Загружаю…'));
    api('/api/admin/ticket/'+id).then(function(t){
      if(fail(t)) return; clear(body);
      if(t.error){body.appendChild(note(errText(t))); return}
      var ul=el('ul','tlist');
      function row(a,b){var li=el('li'); li.appendChild(el('span','tc',a)); li.appendChild(el('b',null,b)); ul.appendChild(li)}
      row('Статус',ST[t.status]||t.status); row('Тип',t.cat); row('Игрок',(t.uname?'@'+t.uname+' · ':'')+'ID '+(t.user||'?'));
      row('Хелпер сейчас',t.admin?(t.admin_name||('ID '+t.admin))+(t.agent?' · агент #'+t.agent:''):(t.status==='pending'?'не назначен':'—'));
      if(t.chain&&t.chain.length>1) row('Цепочка передачи',t.chain.map(function(h){return h.name}).join(' → '));
      row('Создано',dt(t.created)); row('Обновлено',dt(t.updated)); if(t.score) row('Оценка игрока','⭐ '+t.score+' / 5');
      body.appendChild(ul);
      if(t.review) {body.appendChild(el('h3','cab-sub','Отзыв игрока')); body.appendChild(el('div','msg user',t.review))}
      if(t.targets&&t.targets.length) body.appendChild(note('Ники в жалобе: '+t.targets.join(', '),'cab-note small'));
      if(t.link){var a=link('cab-a','Открыть карточку в Telegram',t.link); a.target='_blank'; a.rel='noopener'; body.appendChild(a)}
      body.appendChild(el('h3','cab-sub','Первое сообщение (карточка)'));
      body.appendChild(el('pre','adm-card',t.card||'(пусто)'));
      body.appendChild(el('h3','cab-sub','Переписка'));
      if(!t.messages.length) body.appendChild(note('Переписка этого обращения не сохранена: бот начал записывать сообщения после обновления. Для старых обращений доступны карточка, хелпер, оценка и отзыв.','cab-note small'));
      var conv=el('div','conv'); t.messages.forEach(function(m){conv.appendChild(bubble(m))}); body.appendChild(conv);
      if(t.status==='active'){
        body.appendChild(el('h3','cab-sub','Ответить игроку'));
        var ta=el('textarea'); ta.rows=4; ta.maxLength=3500; ta.placeholder='Текст ответа. Игрок получит его от «Агента #N», как обычный ответ из группы.'; ta.setAttribute('aria-label','Текст ответа');
        var msg=note('','cab-note small'); var row_=el('div','btns');
        var send=btn('Отправить','btn',function(){
          var v=ta.value.trim(); if(!v){msg.textContent='Введите текст.'; return}
          send.disabled=true; msg.textContent='Отправляю…';
          api('/api/admin/ticket/'+id+'/reply',{text:v}).then(function(r){send.disabled=false; if(fail(r)) return;
            if(r.ok){openTicket(id)} else msg.textContent=errText(r)});
        });
        var close=btn('Закрыть обращение','btn alt',function(){
          if(!confirm('Закрыть обращение №'+id+'? Игроку придёт просьба оценить поддержку.')) return;
          close.disabled=true; api('/api/admin/ticket/'+id+'/close',{}).then(function(r){close.disabled=false; if(fail(r)) return;
            if(r.ok){openTicket(id)} else msg.textContent=errText(r)});
        });
        row_.appendChild(send); row_.appendChild(close); body.appendChild(ta); body.appendChild(row_); body.appendChild(msg);
      } else if(t.status==='pending') body.appendChild(note('Обращение ещё никто не взял: сначала его берёт хелпер в группе поддержки.','cab-note small'));
    });
  }

  // ---------- управление ----------
  function tabControl(){
    api('/api/admin/overview').then(function(d){
      if(fail(d)) return; clear(pane); if(d.error){pane.appendChild(note(errText(d))); return}
      var f=d.flags||{};
      function section(title,text){var c=card(title); if(text) c.appendChild(note(text)); pane.appendChild(c); return c}
      var msg=note('','cab-note small');

      var p=section('Публикации в канал',f.paused?'Сейчас ⏸ ОСТАНОВЛЕНЫ: новые посты бота в канал не выходят, поддержка работает.':'Сейчас публикации идут. Остановка блокирует все новые посты бота в канал, поддержка продолжает работать.');
      p.appendChild(btn(f.paused?'▶️ Возобновить публикации':'⏸ Остановить публикации','btn',function(){
        if(!confirm(f.paused?'Возобновить публикации?':'Остановить все новые публикации в канал?')) return;
        api('/api/admin/pause',{on:!f.paused}).then(function(r){if(fail(r)) return; if(r.ok) tabControl(); else alert(errText(r))})}));

      var r=section('Наборы','Открытие набора отправляет уведомление подписчикам, как в боте.');
      [['helper','Набор в хелперы',f.helpers_open],['secret','Набор в искатели секреток',f.seekers_open]].forEach(function(x){
        var row=el('div','adm-row'); row.appendChild(el('span',null,x[1]+': '+(x[2]?'открыт':'закрыт')));
        row.appendChild(btn(x[2]?'Закрыть':'Открыть',null,function(){
          if(!confirm((x[2]?'Закрыть':'Открыть')+' набор: '+x[1]+'?')) return;
          api('/api/admin/recruit',{kind:x[0],open:!x[2]}).then(function(r2){if(fail(r2)) return; if(r2.ok) tabControl(); else alert(errText(r2))})}));
        r.appendChild(row);
      });

      var e=section('Праздничный режим','Меняет оформление сайта и добавляет строку в приветствие бота. Сейчас: '+(f.event||'выключен')+'.');
      var sel=el('select'); sel.setAttribute('aria-label','Режим'); [['','Выключить'],['halloween','🎃 Хэллоуин'],['newyear','🎄 Новый год']].forEach(function(o){var op=el('option',null,o[1]); op.value=o[0]; sel.appendChild(op)}); sel.value=f.event||'';
      var days=el('input'); days.type='number'; days.min=1; days.max=60; days.value=7; days.setAttribute('aria-label','Дней');
      var row=el('div','adm-form'); row.appendChild(sel); row.appendChild(days); row.appendChild(el('span','tm','дней'));
      row.appendChild(btn('Применить','btn',function(){api('/api/admin/event',{mode:sel.value,days:parseInt(days.value,10)||7}).then(function(r3){if(fail(r3)) return; if(r3.ok) tabControl(); else alert(errText(r3))})}));
      e.appendChild(row);

      var o=section('Режимы бота','Технический перерыв: '+(f.maintenance?'включён':'выключен')+'. Тест-режим: '+(f.test_mode?'включён':'выключен')+'. Эти два режима, как и состав главных админов, по-прежнему меняются в боте и только владельцем.');
      o.appendChild(link('cab-a','Журнал действий в кабинете →','/cabinet'));
      pane.appendChild(msg);
    });
  }

  // ---------- канал ----------
  function tabChannel(){
    function load(force){
      api('/api/admin/channel'+(force?'?force=1':'')).then(function(d){
        if(fail(d)) return; clear(pane); var c=card('Аналитика канала'); pane.appendChild(c);
        if(d.error){c.appendChild(note(typeof d.error==='string'&&d.error.length>30?d.error:errText(d)))} else {
          var g=d.growth||{};
          c.appendChild(tiles([[d.subs===null||d.subs===undefined?'—':d.subs,'подписчиков'],[g[7]!==undefined?(g[7]>0?'+':'')+g[7]:'—','за 7 дней'],[g[30]!==undefined?(g[30]>0?'+':'')+g[30]:'—','за 30 дней'],[d.posts_7,'постов за 7 дней'],[d.avg_views_30,'средние просмотры'],[d.avg_fwd_30,'средние пересылки']]));
          if(d.best_hours&&d.best_hours.length){c.appendChild(el('h3','cab-sub','Лучшие часы публикации (МСК)')); var u=el('ul','tlist'); d.best_hours.forEach(function(h){var li=el('li'); li.appendChild(el('span','tc',('0'+h[0]).slice(-2)+':00')); li.appendChild(el('b',null,h[1]+' просмотров')); li.appendChild(el('span','tm',h[2]+' постов')); u.appendChild(li)}); c.appendChild(u)}
          if(d.best_days&&d.best_days.length) c.appendChild(note('Лучшие дни: '+d.best_days.map(function(x){return x[0]+' ('+x[1]+')'}).join(', '),'cab-note small'));
          if(d.top&&d.top.length){c.appendChild(el('h3','cab-sub','Лучшие посты за 30 дней')); var u2=el('ul','tlist'); d.top.forEach(function(p){var li=el('li'); var a=link('cab-a',p.date,p.url); a.target='_blank'; a.rel='noopener'; var s=el('span','tc'); s.appendChild(a); li.appendChild(s); li.appendChild(el('b',null,p.views+' просмотров')); li.appendChild(el('span','tm',p.forwards+' пересылок')); u2.appendChild(li)}); c.appendChild(u2)}
          c.appendChild(note('Обновлено: '+d.updated+'. Рост считается по ежедневным снимкам числа подписчиков, поэтому за первые дни данных мало.','cab-note small'));
        }
        c.appendChild(btn('🔄 Обновить','btn alt',function(){clear(pane); pane.appendChild(note('Загружаю…')); load(true)}));
      });
    }
    load(false);
  }

  var h=(location.hash||'').slice(1); if(['overview','tickets','control','channel'].indexOf(h)>=0) state.tab=h;
  shell(); render();   // без предварительного запроса: права проверит первый же запрос вкладки
})();
