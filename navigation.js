const tabs=[...document.querySelectorAll('[role="tab"][data-app-tab]')];
const panels=[...document.querySelectorAll('[data-app-panel]')];
const aliases={matchPicker:'matches',results:'matches',matchResearch:'matches',calculator:'matches',
  predictionHistory:'history',automationForecasts:'history',qualityDashboard:'quality',qualityNightly:'quality',trainingStatus:'quality',backtesting:'quality',serviceStatus:'data'};
let active=null;

function selectFromHash({scroll=false}={}){
  let fragment='';try{fragment=decodeURIComponent(location.hash.slice(1));}catch{/* Invalid fragments open the default view. */}
  const key=aliases[fragment]||fragment;
  const next=tabs.some(tab=>tab.dataset.appTab===key)?key:'matches';
  for(const panel of panels)panel.hidden=panel.id!==next;
  for(const tab of tabs){const selected=tab.dataset.appTab===next;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;}
  const previous=active;active=next;
  if(previous!==next)window.dispatchEvent(new CustomEvent('footpredict:tab-selected',{detail:{tab:next,previous}}));
  if(scroll||fragment&&aliases[fragment])requestAnimationFrame(()=>{
    const target=document.getElementById(fragment)||document.getElementById(next);
    const details=target?.closest('details');if(details)details.open=true;
    target?.scrollIntoView({block:'start',behavior:'instant'});
  });
}

for(const tab of tabs){
  tab.addEventListener('click',event=>{
    event.preventDefault();
    const hash='#'+tab.dataset.appTab;
    if(location.hash!==hash)history.pushState(null,'',hash);
    selectFromHash({scroll:true});
  });
  tab.addEventListener('keydown',event=>{
    if(event.key===' '||event.key==='Enter'){event.preventDefault();tab.click();return;}
    const index=tabs.indexOf(tab);
    const next=event.key==='ArrowRight'?(index+1)%tabs.length:event.key==='ArrowLeft'?(index+tabs.length-1)%tabs.length:event.key==='Home'?0:event.key==='End'?tabs.length-1:null;
    if(next===null)return;event.preventDefault();tabs[next].focus();tabs[next].click();
  });
}
window.addEventListener('hashchange',()=>selectFromHash({scroll:true}));
window.addEventListener('popstate',()=>selectFromHash({scroll:true}));
selectFromHash();
