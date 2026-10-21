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
      show(card.querySelector('.btn'), open);
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

  var b=document.getElementById('burger'), l=document.getElementById('links');
  if(b&&l){b.addEventListener('click',function(){var o=l.classList.toggle('open');b.setAttribute('aria-expanded',o)});
    l.addEventListener('click',function(e){if(e.target.tagName==='A')l.classList.remove('open')})}
})();
