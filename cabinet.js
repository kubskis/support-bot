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
      var info={code:d.code,secret:d.secret,num:d.num,link:d.link,t:Date.now(),ttl:(d.ttl||600)*1000};
      store(info); viewWait(info);
    }).catch(function(e){
      viewLogin(e==='rate'?'Слишком много попыток. Подождите несколько минут.':'Не удалось начать вход. Попробуйте ещё раз чуть позже.');
    });
  }
  function viewWait(info){
    clear();
    var c=card('Подтвердите вход в Telegram');
    if(info.num){
      c.appendChild(note('Ваше число для сверки (никому его не сообщайте):','cab-note small'));
      var big=el('div','loginnum',String(info.num)); big.setAttribute('aria-label','Число для сверки '+info.num); c.appendChild(big);
    }
    var ol=el('ol','cab-steps');
    ol.appendChild(el('li',null,'Нажмите «Открыть Telegram».'));
    ol.appendChild(el('li',null,info.num?'В чате с ботом нажмите кнопку с числом '+info.num+'.':'В чате с ботом подтвердите вход.'));
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


  // ---------- блоки для команды (приходят только если сервер подтвердил роль) ----------
  function bars(dist,total,labelFn){
    var wrap=el('div','bars');
    ['5','4','3','2','1'].forEach(function(k){
      var n=dist[k]||0, row=el('div','brow');
      row.appendChild(el('span','bl',labelFn?labelFn(k):k+' ★'));
      var b=el('span','bar'); var f=el('i'); f.style.width=(total?Math.round(n*100/total):0)+'%'; b.appendChild(f); row.appendChild(b);
      row.appendChild(el('span','bn',String(n))); wrap.appendChild(row);
    });
    return wrap;
  }
  function tiles(list){var g=el('div','stats'); list.forEach(function(x){g.appendChild(stat(x[0],x[1]))}); return g}
  function rankText(r,of){return r?('#'+r+' из '+of):'—'}
  function fmtDT(ts){ if(!ts) return ''; try{return new Date(ts*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'})}catch(e){return ''} }
  var MEDAL=['🥇','🥈','🥉'];

  function helperCard(h){
    var c=card('Моя статистика хелпера'); c.classList.add('staff');
    c.appendChild(tiles([[h.agent?('#'+h.agent):'—','номер агента'],[rankText(h.rank,h.of),'место в рейтинге'],[h.closed,'закрыто всего'],[h.closed7,'за 7 дней'],[h.closed30,'за 30 дней'],[h.active,'в работе сейчас'],[h.rated?h.avg+' / 5':'—','средняя оценка ('+h.rated+')']]));
    if(h.rated){c.appendChild(el('h3','cab-sub','Оценки игроков')); c.appendChild(bars(h.dist,h.rated))}
    if(h.reviews&&h.reviews.length){
      c.appendChild(el('h3','cab-sub','Последние отзывы'));
      var ul=el('ul','tlist');
      h.reviews.forEach(function(r){var li=el('li'); li.appendChild(el('b',null,'№'+r.ticket)); li.appendChild(el('span','pill2 '+(r.score>=4?'st-done':(r.score>=3?'st-wait':'st-no')),r.score+'/5')); li.appendChild(el('span','tm',fmtDT(r.at))); li.appendChild(el('span','rv',r.text)); ul.appendChild(li)});
      c.appendChild(ul); c.appendChild(note('Отзывы приходят без имён игроков.','cab-note small'));
    }
    return c;
  }
  var BADGE={saboteur:'Саботёр (серверы)',farm:'Фарм (серверы)',mutator:'Бейдж «Мутатор»',researcher:'Бейдж «Исследователь»'};
  function publisherCard(p){
    var c=card('Статистика публикатора'); c.classList.add('staff');
    c.appendChild(tiles([[p.total,'секреток всего'],[p.day,'за 24 часа'],[rankText(p.rank,p.of),'место в топе']]));
    var ul=el('ul','tlist');
    Object.keys(BADGE).forEach(function(k){var o=(p.other||{})[k]||{total:0,day:0}; var li=el('li'); li.appendChild(el('span','tc',BADGE[k])); li.appendChild(el('b',null,String(o.total))); li.appendChild(el('span','tm','за 24 ч: '+o.day)); ul.appendChild(li)});
    c.appendChild(el('h3','cab-sub','Другие публикации')); c.appendChild(ul);
    if(p.types&&p.types.length){
      c.appendChild(el('h3','cab-sub','Любимые типы секреток'));
      var u2=el('ul','tlist'); p.types.forEach(function(t){var li=el('li'); li.appendChild(el('span','tc',t.type)); li.appendChild(el('b',null,String(t.count))); u2.appendChild(li)}); c.appendChild(u2);
    }
    if(p.recent&&p.recent.length){
      c.appendChild(el('h3','cab-sub','Последние секретки'));
      var u3=el('ul','tlist'); p.recent.forEach(function(r){var li=el('li'); li.appendChild(el('span','tc',r.type)); li.appendChild(el('span','pill2 '+(r.expired?'st-no':'st-done'),r.expired?'Закрыта':'Идёт')); li.appendChild(el('span','tm',fmtDT(r.at))); u3.appendChild(li)}); c.appendChild(u3);
    }
    return c;
  }
  var CATN={complaint:'Жалобы',appeal:'Обжалования',question:'Вопросы',friends:'Друзья'};
  function chart(days){
    var max=1; days.forEach(function(d){max=Math.max(max,d.new,d.closed)});
    var wrap=el('div','chart'); wrap.setAttribute('role','img');
    var tn=0,tc=0; days.forEach(function(d){tn+=d.new; tc+=d.closed});
    wrap.setAttribute('aria-label','Обращения за 14 дней: новых '+tn+', закрыто '+tc);
    days.forEach(function(d){
      var col=el('div','ccol'); col.title=d.d+': новых '+d.new+', закрыто '+d.closed;
      var pair=el('div','cpair'); var a=el('i','cnew'); a.style.height=Math.round(d.new*100/max)+'%'; var b=el('i','cdone'); b.style.height=Math.round(d.closed*100/max)+'%'; pair.appendChild(a); pair.appendChild(b);
      col.appendChild(pair); col.appendChild(el('span','cd',d.d.slice(8))); wrap.appendChild(col);
    });
    var box=el('div'); box.appendChild(wrap);
    var lg=el('p','cab-note small'); lg.appendChild(el('span','lg lg-new','новые')); lg.appendChild(document.createTextNode(' ')); lg.appendChild(el('span','lg lg-done','закрытые')); lg.appendChild(document.createTextNode(' · за 14 дней, по Москве')); box.appendChild(lg);
    return box;
  }
  function adminCards(a){
    var out=[];
    var gate=card('Админка'); gate.classList.add('staff','wide');
    gate.appendChild(note('Все обращения с поиском, архив ответов по хелперам, ответ игроку и закрытие, пауза публикаций, наборы и аналитика канала.'));
    gate.appendChild(link('btn','Открыть админку','/admin')); out.push(gate);
    var team=card('Общий рейтинг команды'); team.classList.add('staff','wide');
    if(a.team&&a.team.length){
      var ul=el('ul','tlist trank');
      a.team.forEach(function(t,i){
        var li=el('li'); li.appendChild(el('span','medal',MEDAL[i]||String(i+1)+'.'));
        li.appendChild(el('span','tc',t.name+(t.agent?' · агент #'+t.agent:'')));
        li.appendChild(el('b',null,t.rated?t.avg+' / 5':'—')); li.appendChild(el('span','tm','закрыто '+t.closed+', оценок '+t.rated)); ul.appendChild(li);
      });
      team.appendChild(ul);
    } else team.appendChild(note('Пока нет закрытых обращений.'));
    team.appendChild(note('Рейтинг: сначала средняя оценка, затем число закрытых обращений. Оценки от стаффа не учитываются.','cab-note small'));
    out.push(team);

    var pr=card('Статистика проекта'); pr.classList.add('staff','wide');
    var t=a.tickets||{};
    pr.appendChild(tiles([[a.users,'пользователей в боте'],[t.total,'обращений всего'],[t.closed,'закрыто'],[t.rejected,'отклонено'],[t.pending,'ждут ответа'+(t.pending&&t.oldest_wait_h?' (старое '+t.oldest_wait_h+' ч)':'')],[t.active,'в работе'],[t.avg?t.avg+' / 5':'—','общая оценка']]));
    pr.appendChild(el('h3','cab-sub','Обращения по дням')); pr.appendChild(chart(a.days||[]));
    var cats=a.categories||{}; var u1=el('ul','tlist');
    Object.keys(CATN).forEach(function(k){if(cats[k]!==undefined){var li=el('li'); li.appendChild(el('span','tc',CATN[k])); li.appendChild(el('b',null,String(cats[k]))); u1.appendChild(li)}});
    if(u1.children.length){pr.appendChild(el('h3','cab-sub','По типам обращений')); pr.appendChild(u1)}
    var totalR=0; ['1','2','3','4','5'].forEach(function(k){totalR+=(a.dist||{})[k]||0});
    if(totalR){pr.appendChild(el('h3','cab-sub','Оценки игроков (все агенты)')); pr.appendChild(bars(a.dist,totalR))}
    out.push(pr);

    var pb=card('Публикации и деньги'); pb.classList.add('staff','wide');
    var pu=a.published||{}; var u2=el('ul','tlist');
    [['secrets','Секретки'],['saboteur','Серверы саботёра'],['farm','Фарм-серверы'],['mutator','Бейджи «Мутатор»'],['researcher','Бейджи «Исследователь»']].forEach(function(p){
      var o=pu[p[0]]; if(!o) return; var li=el('li'); li.appendChild(el('span','tc',p[1])); li.appendChild(el('b',null,String(o.total)));
      li.appendChild(el('span','tm','за 24 ч: '+o.day+(o.week!==undefined?', за 7 дн.: '+o.week:''))); u2.appendChild(li);
    });
    pb.appendChild(u2);
    if(a.publishers&&a.publishers.length){
      pb.appendChild(el('h3','cab-sub','Топ публикаторов секреток'));
      var u3=el('ul','tlist trank'); a.publishers.forEach(function(x,i){var li=el('li'); li.appendChild(el('span','medal',MEDAL[i]||String(i+1)+'.')); li.appendChild(el('span','tc',x.name)); li.appendChild(el('b',null,String(x.count))); li.appendChild(el('span','tm','за 24 ч: '+x.day)); u3.appendChild(li)}); pb.appendChild(u3);
    }
    var py=a.payments||{};
    pb.appendChild(el('h3','cab-sub','Оплаты и заявки'));
    pb.appendChild(tiles([[(py.week||{}).count||0,'оплат за 7 дней'],[((py.week||{}).stars||0)+' ⭐','сумма за 7 дней'],[(py.month||{}).count||0,'оплат за 30 дней'],[((py.month||{}).stars||0)+' ⭐','сумма за 30 дней'],[(a.apps||{}).helper||0,'заявок хелперов'],[(a.apps||{}).secret||0,'заявок искателей'],[(a.recruit_subs||{}).helper||0,'ждут набора хелперов'],[(a.recruit_subs||{}).secret||0,'ждут набора искателей']]));
    out.push(pb);
    out.push(faqCard(a.faq));
    out.push(logCard());
    return out;
  }
  function faqCard(list){
    var c=card('Полезность ответов на сайте'); c.classList.add('staff','wide');
    if(!list||!list.length){c.appendChild(note('Голосов пока нет. Посетители отмечают на странице «Вопросы», помог ли им ответ.')); return c}
    var ul=el('ul','tlist');
    list.forEach(function(x){
      var li=el('li'), tot=(x.yes||0)+(x.no||0);
      li.appendChild(el('span','tc',x.title));
      li.appendChild(el('b',null,'👍 '+x.yes+'  👎 '+x.no));
      li.appendChild(el('span','tm',tot?Math.round(x.no*100/tot)+'% не помогло':''));
      ul.appendChild(li);
    });
    c.appendChild(ul);
    c.appendChild(note('Сверху вопросы, на которых чаще нажимают «Нет»: их ответы стоит переписать. Голоса анонимны.','cab-note small'));
    return c;
  }
  function logCard(){
    var c=card('Журнал действий'); c.classList.add('staff','wide');
    c.appendChild(note('Кто и что делал в боте: обращения, блокировки, заявки, доступы, переключатели, а также сбои и перезапуски. Видят только главные админы. Записи хранятся 180 дней.','cab-note small'));
    var F=[['','Все'],['tickets','Обращения'],['people','Люди'],['settings','Настройки'],['system','Сбои и запуски']];
    var chips=el('div','chips'), list=el('ul','tlist alog'), msg=note('Загружаю…'), more=el('button','btn alt logmore','Показать ещё');
    more.type='button'; more.hidden=true;
    var group='', last=0, token=0, busy=false, btns=[];
    function load(reset){
      if(reset){token++; busy=false; list.textContent=''; last=0; more.hidden=true}
      if(busy) return; busy=true;
      var my=token; msg.hidden=false; msg.textContent='Загружаю…';
      api('/api/admin/log?g='+encodeURIComponent(group)+(last?'&before='+last:'')).then(function(r){
        if(!r.ok) throw r.status; return r.json();
      }).then(function(d){
        if(my!==token) return;
        (d.items||[]).forEach(function(it){
          var li=el('li',it.group==='system'?'sys':'');
          li.appendChild(el('span','tm',fmtDT(it.at)));
          var tc=el('span','tc'); tc.appendChild(el('b',null,it.who)); tc.appendChild(document.createTextNode(' · '+it.action+(it.target?' · '+it.target:''))); li.appendChild(tc);
          if(it.details) li.appendChild(el('span','rv',it.details));
          list.appendChild(li); last=it.id;
        });
        msg.hidden=list.children.length>0; if(!list.children.length) msg.textContent='Записей пока нет.';
        more.hidden=!d.more;
      }).catch(function(e){
        if(my!==token) return;
        msg.hidden=false; msg.textContent=(e===429)?'Слишком часто. Подождите минуту.':(e===403?'Нет доступа.':'Не удалось загрузить журнал.');
      }).then(function(){ if(my===token) busy=false });
    }
    F.forEach(function(f){
      var b=el('button',null,f[1]); b.type='button'; b.setAttribute('aria-pressed',f[0]===''?'true':'false');
      b.addEventListener('click',function(){
        group=f[0]; btns.forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false')}); load(true);
      });
      btns.push(b); chips.appendChild(b);
    });
    more.addEventListener('click',function(){load(false)});
    c.appendChild(chips); c.appendChild(list); c.appendChild(msg); c.appendChild(more);
    load(true);
    return c;
  }
  function renderStaff(sf){
    if(!sf||!sf.roles||!sf.roles.length) return;
    var h=el('div','cab-sect'); h.appendChild(el('h2',null,'Для команды'));
    var roles={helper:'хелпер',publisher:'публикатор секреток',admin:'главный админ'};
    h.appendChild(note('Ваши роли: '+sf.roles.map(function(r){return roles[r]||r}).join(', ')+'. Этот раздел видите только вы.','cab-note small'));
    root.appendChild(h);
    var grid=el('div','cab-grid');
    if(sf.helper) grid.appendChild(helperCard(sf.helper));
    if(sf.publisher) grid.appendChild(publisherCard(sf.publisher));
    root.appendChild(grid);
    if(sf.admin) adminCards(sf.admin).forEach(function(c){root.appendChild(c)});
  }

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

    renderStaff(d.staff);

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
