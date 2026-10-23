(function(){
  var list=document.querySelectorAll('.faq-full details[data-q]');
  if(!list.length) return;
  var BOT='https://t.me/ToHSecrets_bot?start=go_question';
  var boxes={};
  function el(tag,cls,text){var e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e}

  function mark(q,v){
    var b=boxes[q]; if(!b) return;
    b.yes.setAttribute('aria-pressed',v===1?'true':'false');
    b.no.setAttribute('aria-pressed',v===-1?'true':'false');
    b.yes.classList.toggle('on',v===1); b.no.classList.toggle('on',v===-1);
    b.msg.textContent=''; b.more.hidden=true;
    if(v===1) b.msg.textContent='Спасибо за отзыв!';
    if(v===-1){b.msg.textContent='Спасибо, мы это учтём. '; b.more.hidden=false}
  }
  function send(q,v,btn){
    var b=boxes[q]; b.yes.disabled=b.no.disabled=true;
    fetch('/api/faq/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q:q,v:v}),cache:'no-store'})
      .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d}})})
      .then(function(x){
        b.yes.disabled=b.no.disabled=false;
        if(x.ok&&x.d&&x.d.ok) mark(q,x.d.v);
        else b.msg.textContent=(x.d&&x.d.error==='rate')?'Слишком часто, попробуйте позже.':'Не удалось отправить. Попробуйте позже.';
      })
      .catch(function(){b.yes.disabled=b.no.disabled=false; b.msg.textContent='Нет связи. Попробуйте позже.'});
  }
  Array.prototype.forEach.call(list,function(d){
    var q=d.getAttribute('data-q');
    var box=el('div','vote'); box.appendChild(el('span','vq','Ответ помог?'));
    var yes=el('button','vbtn','👍 Да'), no=el('button','vbtn','👎 Нет'); yes.type=no.type='button';
    yes.setAttribute('aria-pressed','false'); no.setAttribute('aria-pressed','false');
    var msg=el('span','vmsg'); msg.setAttribute('role','status');
    var more=el('a','vmore','Задать вопрос в боте'); more.href=BOT; more.hidden=true;
    yes.addEventListener('click',function(){send(q,boxes[q].yes.classList.contains('on')?0:1)});
    no.addEventListener('click',function(){send(q,boxes[q].no.classList.contains('on')?0:-1)});
    box.appendChild(yes); box.appendChild(no); box.appendChild(msg); box.appendChild(more);
    d.appendChild(box); boxes[q]={yes:yes,no:no,msg:msg,more:more};
  });
  // свои прошлые голоса (чужие и общие счётчики сайт не показывает)
  fetch('/api/faq/votes',{cache:'no-store'}).then(function(r){return r.ok?r.json():null}).then(function(d){
    if(!d||!d.mine) return;
    Object.keys(d.mine).forEach(function(q){
      mark(q,d.mine[q]); if(boxes[q]) boxes[q].msg.textContent='';
      if(boxes[q]) boxes[q].more.hidden=true;
    });
  }).catch(function(){});
  // переход по ссылке /faq#вопрос открывает нужный ответ
  function openHash(){var id=(location.hash||'').slice(1); if(!id) return; var d=document.getElementById(id); if(d&&d.tagName==='DETAILS'){d.open=true; d.scrollIntoView({block:'center'})}}
  openHash(); window.addEventListener('hashchange',openHash);
})();
