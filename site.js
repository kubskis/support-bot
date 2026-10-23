(function(){
  var dot=document.getElementById('dot'), st=document.getElementById('st');
  function set(c,t){if(dot){dot.className='dot '+c;st.textContent=t}}
  function show(el,on){if(el){if(on){el.removeAttribute('hidden')}else{el.setAttribute('hidden','')}}}

  // карточки набора: статус берётся из настроек бота
  function applyRecruit(r){
    document.querySelectorAll('[data-recruit]').forEach(function(card){
      var open = r ? r[card.getAttribute('data-recruit')] : undefined;
      if(open!==true && open!==false) return;          // статус неизвестен: оставляем как есть
      show(card.querySelector('.pill.ok'), open);
      show(card.querySelector('.pill.off'), !open);
      show(card.querySelector('.btn:not(.notify)'), open);
      show(card.querySelector('.notify'), !open);
      show(card.querySelector('.closed-note'), !open);
    });
  }
  // функции, которые бывают выключены в боте (например, рефералка)
  function applyFeatures(d){
    document.querySelectorAll('[data-feature]').forEach(function(el){
      if(d && d[el.getAttribute('data-feature')]===true) el.classList.add('on');
    });
  }

  fetch('/api/status',{cache:'no-store'}).then(function(r){return r.json()}).then(function(d){
    var ok=d&&d.ok&&(d.health==='ok'||d.bot==='up');set(ok?'ok':'bad',ok?'бот работает':'есть проблемы');
    applyRecruit(d&&d.recruit); applyFeatures(d);
  }).catch(function(){set('bad','нет связи')});

  // лента секреток (только тип, время и статус; ссылок на серверы тут нет)
  function ago(sec){
    if(sec<60) return 'только что';
    if(sec<3600) return Math.floor(sec/60)+' мин назад';
    return Math.floor(sec/3600)+' ч назад';
  }
  function el(tag,cls,text){var e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e}
  function loadFeed(){
    var sec=document.getElementById('live'), list=document.getElementById('feed');
    if(!sec||!list) return;
    fetch('/api/secrets',{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(d){
      if(!d||d.error) return;          // база не ответила: не показываем «секреток не было» зря
      var items=(d&&d.items)||[];
      list.textContent='';
      items.forEach(function(it){
        var li=el('li',it.open?'open':'closed');
        li.appendChild(el('span','ft',it.type));
        li.appendChild(el('span','fs',it.open?'Идёт сейчас':'Закрыта'));
        li.appendChild(el('span','fa',ago(it.age)));
        list.appendChild(li);
      });
      show(document.getElementById('feed-empty'), items.length===0);
      show(list, items.length>0);
      var st=document.getElementById('live-stats'); st.textContent='';
      var has=false;
      [['farm','Фарм-серверов сейчас: '],['saboteur','Серверов саботёра сейчас: ']].forEach(function(p){
        if(typeof d[p[0]]==='number'){has=true; var s=el('span'); s.appendChild(document.createTextNode(p[1])); s.appendChild(el('b',null,String(d[p[0]]))); st.appendChild(s)}
      });
      show(st,has);
      show(sec,true);
    }).catch(function(){/* нет данных: блок остаётся скрытым */});
  }
  loadFeed();
  if(document.getElementById('feed')) setInterval(function(){if(!document.hidden) loadFeed()},30000);

  // витрина услуг: берётся из бота, обновляется вместе с ним
  function loadShop(){
    var sec=document.getElementById('shop'), grid=document.getElementById('shop-grid');
    if(!sec||!grid) return;
    fetch('/api/shop',{cache:'no-store'}).then(function(r){if(!r.ok)throw 0;return r.json()}).then(function(d){
      if(!d||d.error) return;
      var sv=d.services||[];
      grid.textContent='';
      sv.forEach(function(s){
        var c=el('div','card');
        c.appendChild(el('h3',null,s.title));
        if(s.description) c.appendChild(el('p','desc',s.description));
        var ul=el('ul','tariffs');
        (s.tariffs||[]).forEach(function(t){
          var li=el('li'); li.appendChild(el('span',null,t.label)); li.appendChild(el('b',null,t.stars+' ⭐')); ul.appendChild(li);
        });
        c.appendChild(ul);
        var a=el('a','btn','Купить в боте'); a.href='https://t.me/ToHSecrets_bot?start=go_shop_'+encodeURIComponent(s.id);
        c.appendChild(a);
        grid.appendChild(c);
      });
      show(document.getElementById('shop-empty'), sv.length===0);
      show(grid, sv.length>0);
      var ch=document.getElementById('shop-channel'); if(ch&&/^https:\/\//.test(d.channel||'')) ch.href=d.channel;
      show(sec,true);
    }).catch(function(){/* нет данных: остаётся обычное описание ниже */});
  }
  loadShop();

  var b=document.getElementById('burger'), l=document.getElementById('links');
  if(b&&l){b.addEventListener('click',function(){var o=l.classList.toggle('open');b.setAttribute('aria-expanded',o)});
    l.addEventListener('click',function(e){if(e.target.tagName==='A')l.classList.remove('open')})}
})();
