javascript:void(function(){
  try{
    var s=document.title.replace(/ - [^-]+@[^-]+ - Gmail.*$/,'').replace(/^Gmail - /,'');
    var b='';
    var els=document.querySelectorAll('.a3s.aiL, .a3s.aXjCH, div[data-message-id] .a3s');
    if(els.length>0){b=els[els.length-1].innerText.substring(0,2000)}
    else{var gs=document.querySelectorAll('[role="listitem"] .ii.gt');
      if(gs.length>0){b=gs[gs.length-1].innerText.substring(0,2000)}}
    if(!b){var all=document.querySelectorAll('.ii.gt');if(all.length>0){b=all[all.length-1].innerText.substring(0,2000)}}
    var f='';
    var fromEls=document.querySelectorAll('span[email], .gD');
    if(fromEls.length>0){var last=fromEls[fromEls.length-1];f=last.getAttribute('email')||last.innerText}
    var base='http://localhost:8765/feedback';
    var u=base+'?inbound='+encodeURIComponent(b)+'&sender='+encodeURIComponent(f)+'&subject='+encodeURIComponent(s);
    window.open(u,'_blank');
  }catch(e){alert('YouOS bookmarklet error: '+e.message)}
}())
