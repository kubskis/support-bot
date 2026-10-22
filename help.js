(function(){
  var input=document.getElementById('q'), out=document.getElementById('results'),
      chips=document.getElementById('chips'), status=document.getElementById('hstatus'), form=document.getElementById('hform');
  if(!input||!out) return;
  var items=[], loaded=false, failed=false;

  function el(tag,cls,text){var e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e}
  function clean(s){return (s||'').replace(/\s+/g,' ').trim()}
  function noEmoji(s){return clean(s).replace(/^[^\p{L}\p{N}«"]+/u,'')}
  function norm(s){return (s||'').toLowerCase().replace(/ё/g,'е')}
  function words(s){return norm(s).split(/[^a-zа-я0-9]+/).filter(Boolean)}
  // грубое «обрезание окончаний»: «ускорять» находит «ускорение», «ускорять»
  function stem(t){var n=t.length; if(n<=4) return t; if(n<=6) return t.slice(0,n-1); return t.slice(0,n-3)}
  // слова-синонимы: по «бан» находим и «блокировка», по «хелпер» и «набор в команду»
  var SYN={'бан':['заблок','блокир','обжалов','наказан'],'бана':['заблок','блокир','обжалов'],'забанили':['заблок','блокир','обжалов'],
    'хелпер':['хелпер','набор','команд'],'хелпером':['хелпер','набор','команд'],'оплата':['оплат','платеж','stars','звезд'],'купить':['оплат','услуг','stars'],
    'жалоба':['жалоб','нарушител','репорт'],'мут':['мут','наказан'],'варн':['варн','наказан'],'афк':['афк'],'нимб':['нимб']};
  function wordMatch(w,t){
    if(t.length<=3) return w===t || (w.indexOf(t)===0 && w.length<=t.length+2);
    return w.indexOf(t)===0;
  }

  function add(cat,title,body,url){
    title=clean(title); body=clean(body); if(!title) return;
    items.push({cat:cat,title:title,body:body,url:url,tw:words(title),bw:words(body+' '+cat)});
  }
  function parseRules(doc){
    doc.querySelectorAll('.rules-sec').forEach(function(sec){
      var h2=sec.querySelector('h2'), secTitle=noEmoji(h2?h2.textContent:'Правила');
      var cur={id:sec.id,title:secTitle,isServer:sec.id==='servers'};
      sec.querySelectorAll('.rtype, .rule').forEach(function(n){
        if(n.classList.contains('rtype')){cur={id:n.id,title:noEmoji(n.textContent),isServer:true}; return}
        var h=n.querySelector('h3'), p=n.querySelector('p'); if(!h) return;
        var hc=h.cloneNode(true); var e=hc.querySelector('.e'); if(e) e.remove();
        add(cur.isServer?'Правила: '+cur.title:'Правила чата', hc.textContent, p?p.textContent:'', '/rules#'+(cur.id||''));
      });
    });
  }
  function parseWarn(doc){
    doc.querySelectorAll('.rules-sec').forEach(function(sec){
      var w=sec.querySelector('.warn span:last-child'), h2=sec.querySelector('h2');
      if(w) add('Наказания','Наказание за нарушение: '+noEmoji(h2?h2.textContent:'правила'),w.textContent+' Ошибочный бан можно обжаловать в боте.','/rules#'+sec.id);
    });
  }
  function parseHome(doc){
    doc.querySelectorAll('#support .card').forEach(function(c){
      var h=c.querySelector('h3'), p=c.querySelector('p'); if(h) add('Поддержка',h.textContent,p?p.textContent:'','/#support');
    });
    doc.querySelectorAll('#team .card').forEach(function(c){
      var h=c.querySelector('h3'), p=c.querySelector('p'); if(h) add('Набор в команду','Набор: '+h.textContent,(p?p.textContent:'')+' Статус набора смотрите на главной странице.','/#team');
    });
  }
  function parseFaq(doc){
    doc.querySelectorAll('#faq details').forEach(function(d){
      var s=d.querySelector('summary'), p=d.querySelector('p');
      if(s) add('Частые вопросы', s.textContent, p?p.textContent:'', '/#faq');
    });
  }
  function parseProse(doc,cat,url){
    var pr=doc.querySelector('.prose'); if(!pr) return;
    var cur=null;
    Array.prototype.forEach.call(pr.children,function(n){
      if(n.tagName==='H2'){ if(cur) add(cat,cur.t,cur.b.join(' '),url); cur={t:n.textContent,b:[]}; }
      else if(cur && !n.classList.contains('btns')){ cur.b.push(n.textContent); }
    });
    if(cur) add(cat,cur.t,cur.b.join(' '),url);
  }
  function get(url){return fetch(url,{cache:'no-cache'}).then(function(r){return r.ok?r.text():''}).catch(function(){return ''})}
  function doc(html){return html?new DOMParser().parseFromString(html,'text/html'):null}

  // база поиска строится из самих страниц сайта: изменили правила — поиск обновился сам
  Promise.all([get('/rules'),get('/'),get('/services'),get('/privacy')]).then(function(r){
    var d;
    if((d=doc(r[0]))) {parseRules(d); parseWarn(d);}
    if((d=doc(r[1]))) {parseFaq(d); parseHome(d);}
    if((d=doc(r[2]))) parseProse(d,'Платные услуги','/services');
    if((d=doc(r[3]))) parseProse(d,'Конфиденциальность','/privacy');
    loaded=true; failed=items.length===0; render();
  });

  function search(q){
    var toks=words(q).filter(function(t){return t.length>=2}).map(function(t){return {raw:t,alts:[stem(t)].concat(SYN[t]||[])}});
    if(!toks.length) return [];
    var res=[];
    items.forEach(function(it,i){
      var score=0;
      for(var k=0;k<toks.length;k++){
        var alts=toks[k].alts, s=0;
        for(var a=0;a<alts.length;a++){
          var t=alts[a], own=(a===0);
          var sc = it.tw.some(function(w){return wordMatch(w,t)}) ? 3 : (it.bw.some(function(w){return wordMatch(w,t)}) ? 1 : 0);
          if(!own && sc) sc=Math.max(1,sc-1);     // синоним весит чуть меньше самого слова
          if(sc>s) s=sc;
        }
        if(!s) return;            // каждое слово запроса должно найтись
        score+=s;
      }
      res.push({it:it,score:score,i:i});
    });
    res.sort(function(a,b){return b.score-a.score||a.i-b.i});
    return res.slice(0,12).map(function(x){return x.it});
  }

  var BOT='https://t.me/ToHSecrets_bot?start=go_question';
  function render(){
    var q=clean(input.value);
    try{ var u=new URL(location.href); if(q) u.searchParams.set('q',q); else u.searchParams.delete('q'); history.replaceState(null,'',u.pathname+u.search+u.hash) }catch(e){}
    out.textContent='';
    if(!loaded){status.textContent='Загружаю…'; return}
    if(failed){
      status.textContent='';
      out.appendChild(el('p','h-empty','Не удалось загрузить базу поиска. Откройте правила или задайте вопрос в боте.'));
      var b=el('div','btns'); var a1=el('a','btn','Открыть правила'); a1.href='/rules'; var a2=el('a','btn alt','Задать вопрос'); a2.href=BOT; b.appendChild(a1); b.appendChild(a2); out.appendChild(b); return;
    }
    if(chips) chips.hidden=!!q;
    if(!q){status.textContent='Введите слово или фразу, например «афк» или «бан».'; return}
    var r=search(q);
    if(!r.length){
      status.textContent='Ничего не найдено.';
      out.appendChild(el('p','h-empty','По запросу «'+q+'» ничего нет. Попробуйте другое слово или спросите команду: ответ придёт прямо в чат с ботом.'));
      var b2=el('div','btns'); var a3=el('a','btn','Задать вопрос в боте'); a3.href=BOT; b2.appendChild(a3); out.appendChild(b2); return;
    }
    status.textContent='Найдено: '+r.length+(r.length===12?' (показаны лучшие)':'');
    var ul=el('ul','res-list');
    r.forEach(function(it){
      var li=el('li'), a=el('a','res'); a.href=it.url;
      a.appendChild(el('span','res-cat',it.cat));
      a.appendChild(el('span','res-title',it.title));
      if(it.body){var s=it.body.length>180?it.body.slice(0,177).replace(/\s+\S*$/,'')+'…':it.body; a.appendChild(el('span','res-body',s))}
      li.appendChild(a); ul.appendChild(li);
    });
    out.appendChild(ul);
  }

  input.addEventListener('input',render);
  if(form) form.addEventListener('submit',function(e){e.preventDefault(); render()});
  if(chips) chips.addEventListener('click',function(e){
    var b=e.target.closest('button'); if(!b) return;
    input.value=b.textContent; render(); input.focus();
  });
  try{ var q0=new URL(location.href).searchParams.get('q'); if(q0) input.value=q0.slice(0,80) }catch(e){}
  render();
})();
