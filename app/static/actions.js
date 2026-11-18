const archive={busy:false,record:JSON.parse(document.getElementById('archive').textContent)};const notice=document.getElementById('notice');async function validateCertificate(url,documentBody){if(archive.busy)return;archive.busy=true;notice.textContent='正在核验溯源链…';document.querySelectorAll('button').forEach(b=>b.disabled=true);try{const options={method:'POST'};options.body=documentBody instanceof FormData?documentBody:JSON.stringify(documentBody);if(!(documentBody instanceof FormData))options.headers={'Content-Type':'application/json'};const response=await fetch(url,options);const answer=await response.json();if(!response.ok)throw Error(answer.message);if(answer.manifest){location.href=location.pathname+'?run_id='+encodeURIComponent(answer.manifest.run_id)}else{location.reload()}}catch(error){notice.textContent=error.message}finally{archive.busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false)}}
document.getElementById('seed').addEventListener('click',()=>validateCertificate('/api/run',{}));document.getElementById('calc').addEventListener('click',()=>validateCertificate('/api/run',{csv:document.getElementById('certs').value}));document.querySelectorAll('[data-decision]:not([data-m8-action])').forEach(b=>b.addEventListener('click',()=>validateCertificate('/api/review',{...archive.record,decision:b.dataset.decision})));document.getElementById('upload').addEventListener('click',()=>{const file=document.getElementById('certificate').files[0];if(!file){notice.textContent='选择校准档案后上传。';return}const packet=new FormData();packet.append('file',file);validateCertificate('/api/run',packet)});document.getElementById('print').addEventListener('click',()=>window.print());

/* M8 证书纠错补录 */
const m8Notice=document.getElementById('m8-notice');
async function m8Request(url,body){
  const options={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
  const response=await fetch(url,options);const answer=await response.json();
  if(!response.ok)throw Error(answer.message||'请求失败');return answer;
}
const m8Submit=document.getElementById('m8-submit');
if(m8Submit){
  const runId=archive.record.id;
  m8Submit.addEventListener('click',async()=>{
    const csv=document.getElementById('correction-csv').value;
    if(!csv.trim()){m8Notice.textContent='请粘贴纠错CSV。';return;}
    try{
      const answer=await m8Request('/api/m8/corrections',{run_id:runId,csv});
      location.href='/work/8?run_id='+encodeURIComponent(runId)+'&correction_id='+encodeURIComponent(answer.correction_id);
    }catch(e){m8Notice.textContent=e.message;}
  });
  document.getElementById('m8-upload').addEventListener('click',async()=>{
    const file=document.getElementById('correction-file').files[0];
    if(!file){m8Notice.textContent='选择纠错CSV后上传。';return;}
    const packet=new FormData();packet.append('file',file);packet.append('run_id',runId);
    try{
      const response=await fetch('/api/m8/corrections',{method:'POST',body:packet});
      const answer=await response.json();if(!response.ok)throw Error(answer.message);
      location.href='/work/8?run_id='+encodeURIComponent(runId)+'&correction_id='+encodeURIComponent(answer.correction_id);
    }catch(e){m8Notice.textContent=e.message;}
  });
  document.querySelectorAll('[data-m8-action]').forEach(button=>{
    button.addEventListener('click',async()=>{
      const cid=archive.record.correction_id;
      if(!cid){m8Notice.textContent='请先提交纠错。';return;}
      const action=button.dataset.m8Action;
      try{
        if(action==='rollback'){
          await m8Request('/api/m8/corrections/'+cid+'/rollback',{revision:archive.record.correction_revision});
        }else{
          await m8Request('/api/m8/corrections/'+cid+'/confirm',{revision:archive.record.correction_revision,decision:button.dataset.decision});
        }
        location.reload();
      }catch(e){m8Notice.textContent=e.message;}
    });
  });
}
