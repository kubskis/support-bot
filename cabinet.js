(function(){
  var root=document.getElementById('cab'); if(!root) return;
  var KEY='tos_login', pollTimer=null;
  var BOT='https://t.me/ToHSecrets_bot';

  function el(tag,cls,text){var e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e}
  function link(cls,text,href,ext){var a=el('a',cls,text); a.href=href; if(ext){a.target='_blank'; a.rel='noopener'} return a}
  function api(path,opts){return fetch(path,Object.assign({cache:'no-store',credentials:'same-origin'},opts||{}))}
  function store(v){try{ if(v) sessionStorage.setItem(KEY,JSON.stringify(v)); else sessionStorage.removeItem(KEY) }catch(e){}}
  function stored(){try{return JSON.parse(sessionStorage.getItem(KEY)||'null')}catch(e){return null}}
  function clear(){ if(pollTimer){clearTimeout(pollTimer); pollTimer=null} root.textContent='' }
  function fmt(ts){ if(!ts) return ''; try{return new Date(ts*1000).toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',timeZone:'Europe/Moscow'})}catch(e){return ''} }

  function card(title){var c=el('section','cab-card'); if(title) c.appendChild(el('h2',null,title)); return c}
  function note(text,cls){return el('p',cls||'cab-note',text)}

  function viewLoading(){clear(); root.appendChild(note('Загружаю…'))}
  function viewError(){
    clear();
    var c=card('Не получилось загрузить'); c.appendChild(note('Сайт не смог связаться с ботом. Попробуйте обновить страницу через минуту.'));
    var b=el('button','btn','Обновить'); b.type='button'; b.addEventListener('click',init); c.appendChild(b); root.appendChild(c);
  }

  // ---------- вход ----------
  function viewLogin(msg){
    clear();
    var c=card('Вход в личный кабинет');
    c.appendChild(note('Войдите через Telegram: без паролей и без передачи данных на сторонние сайты. В кабинете вы увидите статусы своих обращений, заявок, приглашений и услуг.'));
    if(msg) c.appendChild(note(msg,'cab-note cab-warn'));
    var b=el('button','btn','Войти через Telegram'); b.type='button';
    b.addEventListener('click',function(){startLogin(b)});
    c.appendChild(b);
    c.appendChild(note('Вход подтверждается кнопкой в чате с ботом. Сессия хранится в безопасной cookie, выйти можно в любой момент.','cab-note small'));
    root.appendChild(c);
  }
  function startLogin(btn){
    if(btn){btn.disabled=true; btn.textContent='Готовлю ссылку…'}
    api('/api/login/start',{method:'POST'}).then(function(r){
      if(r.status===429) throw 'rate'; if(!r.ok) throw 'fail'; return r.json();
    }).then(function(d){
      var info={code:d.code,secret:d.secret,link:d.link,t:Date.now(),ttl:(d.ttl||600)*1000};
      store(info); viewWait(info);
    }).catch(function(e){
      viewLogin(e==='rate'?'Слишком много попыток. Подождите несколько минут.':'Не удалось начать вход. Попробуйте ещё раз чуть позже.');
    });
  }
  function viewWait(info){
    clear();
    var c=card('Подтвердите вход в Telegram');
    var ol=el('ol','cab-steps');
    ol.appendChild(el('li',null,'Нажмите «Открыть Telegram».'));
    ol.appendChild(el('li',null,'В чате с ботом нажмите «Да, это я, войти».'));
    ol.appendChild(el('li',null,'Вернитесь на эту страницу: вход выполнится сам.'));
    c.appendChild(ol);
    var row=el('div','btns');
    row.appendChild(link('btn','Открыть Telegram',info.link,true));
    var cancel=el('button','btn alt','Отмена'); cancel.type='button';
    cancel.addEventListener('click',function(){store(null); viewLogin()});
    row.appendChild(cancel); c.appendChild(row);
    var st=el('p','cab-note small','Ждём подтверждения…'); st.setAttribute('role','status'); c.appendChild(st);
    root.appendChild(c);
    poll(info,st);
  }
  function poll(info,st){
    function tick(){
      if(Date.now()-info.t>info.ttl){store(null); viewLogin('Время на подтверждение вышло. Нажмите «Войти» ещё раз.'); return}
      api('/api/login/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:info.code,secret:info.secret})})
        .then(function(r){return r.json()}).then(function(d){
          if(d.status==='ok'){store(null); init(); return}
          if(d.status==='wait'){pollTimer=setTimeout(tick,2000); return}
          store(null); viewLogin('Ссылка для входа устарела. Нажмите «Войти» ещё раз.');
        }).catch(function(){ if(st) st.textContent='Нет связи, пробую ещё раз…'; pollTimer=setTimeout(tick,3000) });
    }
    pollTimer=setTimeout(tick,1500);
  }

  // ---------- кабинет ----------
  var CAT={complaint:'Жалоба',appeal:'Обжалование',question:'Вопрос',friends:'Друзья'};
  var ST={pending:['Ждёт ответа','st-wait'],active:['В работе','st-work'],closed:['Закрыто','st-done'],rejected:['Отклонено','st-no']};
  var APP={helper:'Заявка в хелперы',secret:'Заявка в искатели секреток'};

  function stat(n,label){var s=el('div','stat'); s.appendChild(el('b',null,String(n))); s.appendChild(el('span',null,label)); return s}

  function viewCabinet(d){
    clear();
    var head=el('div','cab-head');
    var t=el('div'); t.appendChild(el('h2',null,'Здравствуйте'+(d.user&&d.user.name?', '+d.user.name:'')+'!')); t.appendChild(note('ID в Telegram: '+(d.user?d.user.id:''),'cab-note small'));
    head.appendChild(t);
    var out=el('button','btn alt','Выйти'); out.type='button';
    out.addEventListener('click',function(){api('/api/logout',{method:'POST'}).then(function(){location.reload()},function(){location.reload()})});
    head.appendChild(out); root.appendChild(head);

    if(d.banned){
      var w=el('div','warn'); w.appendChild(el('span','wi','⛔')); 
      var wt=el('span'); wt.appendChild(document.createTextNode('Вы заблокированы в поддержке. ')); if(d.ban_reason){var bb=el('b',null,'Причина: '+d.ban_reason); wt.appendChild(bb); wt.appendChild(document.createTextNode('. '))}
      wt.appendChild(link('cab-a','Обжаловать',BOT+'?start=go_appeal')); w.appendChild(wt); w.firstChild.setAttribute('aria-hidden','true'); root.appendChild(w);
    }
    if(d.subscribed===false){
      var w2=el('div','warn'); var i2=el('span','wi','📢'); i2.setAttribute('aria-hidden','true'); w2.appendChild(i2);
      var s2=el('span'); s2.appendChild(document.createTextNode('Вы не подписаны на канал, поэтому бот может не отвечать. ')); s2.appendChild(link('cab-a','Подписаться','https://t.me/ToHSecretss',true)); w2.appendChild(s2); root.appendChild(w2);
    }

    var grid=el('div','cab-grid');

    // обращения
    var tk=card('Мои обращения');
    if(d.tickets&&d.tickets.length){
      var ul=el('ul','tlist');
      d.tickets.forEach(function(x){
        var li=el('li'); li.appendChild(el('b',null,'№'+x.id));
        li.appendChild(el('span','tc',CAT[x.category]||'Заявка'));
        var s=ST[x.status]||[x.status||'—','st-wait']; li.appendChild(el('span','pill2 '+s[1],s[0]));
        var meta=fmt(x.created)+(x.rating?' · оценка '+x.rating+'/5':''); li.appendChild(el('span','tm',meta));
        ul.appendChild(li);
      });
      tk.appendChild(ul); tk.appendChild(note('Тексты обращений на сайте не показываются: переписка идёт только в боте.','cab-note small'));
    } else tk.appendChild(note('Обращений пока нет.'));
    grid.appendChild(tk);

    // заявки
    if(d.apps&&d.apps.length){
      var ap=card('Мои заявки'); var ul2=el('ul','tlist');
      d.apps.forEach(function(k){var li=el('li'); li.appendChild(el('span','tc',APP[k]||'Заявка')); li.appendChild(el('span','pill2 st-wait','На рассмотрении')); ul2.appendChild(li)});
      ap.appendChild(ul2); grid.appendChild(ap);
    }

    // рефералка
    if(d.ref){
      var rf=card('Приглашения');
      var st=el('div','stats'); st.appendChild(stat(d.ref.confirmed,'подтверждено')); st.appendChild(stat(d.ref.waiting,'ждут подтверждения')); st.appendChild(stat(d.ref.pending,'не подписались')); st.appendChild(stat(d.ref.rewards,'наград получено'));
      rf.appendChild(st);
      if(d.ref.link){
        var row=el('div','copyrow'); var inp=el('input'); inp.type='text'; inp.readOnly=true; inp.value=d.ref.link; inp.setAttribute('aria-label','Ваша реферальная ссылка');
        var cp=el('button','btn','Копировать'); cp.type='button';
        cp.addEventListener('click',function(){
          function done(){cp.textContent='Скопировано ✓'; setTimeout(function(){cp.textContent='Копировать'},2000)}
          if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(inp.value).then(done,function(){inp.select(); try{document.execCommand('copy'); done()}catch(e){}})}
          else{inp.select(); try{document.execCommand('copy'); done()}catch(e){}}
        });
        row.appendChild(inp); row.appendChild(cp); rf.appendChild(row);
      }
      grid.appendChild(rf);
    }

    // предссылки
    var pl=card('Предссылки');
    if(d.prelinks&&d.prelinks.active) pl.appendChild(note('Доступ активен'+(d.prelinks.until?': '+d.prelinks.until:'')+'.'));
    else pl.appendChild(note('Доступа нет. Предссылки присылают секретки раньше канала.'));
    pl.appendChild(link('btn','Услуги в боте',BOT+'?start=go_shop')); grid.appendChild(pl);

    // оплаты
    if(d.payments&&d.payments.length){
      var py=card('Последние оплаты'); var ul3=el('ul','tlist');
      d.payments.forEach(function(p){var li=el('li'); li.appendChild(el('span','tc',p.title+(p.label?' · '+p.label:''))); li.appendChild(el('b',null,p.stars+' ⭐')); li.appendChild(el('span','tm',fmt(p.created))); ul3.appendChild(li)});
      py.appendChild(ul3); grid.appendChild(py);
    }
    root.appendChild(grid);

    var act=card('Быстрые действия'); var row2=el('div','btns');
    row2.appendChild(link('btn','Подать жалобу',BOT+'?start=go_complaint'));
    row2.appendChild(link('btn alt','Задать вопрос',BOT+'?start=go_question'));
    row2.appendChild(link('btn alt','Обжаловать бан',BOT+'?start=go_appeal'));
    act.appendChild(row2); root.appendChild(act);
  }

  function init(){
    viewLoading();
    api('/api/me').then(function(r){
      if(r.status===401) return null;
      if(!r.ok) throw 0; return r.json();
    }).then(function(d){
      if(d){viewCabinet(d); return}
      var s=stored();
      if(s&&s.code&&Date.now()-s.t<s.ttl) viewWait(s); else viewLogin();
    }).catch(viewError);
  }
  init();
})();
