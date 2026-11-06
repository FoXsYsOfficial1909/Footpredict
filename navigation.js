const tabs=[...document.querySelectorAll('[role="tab"][data-app-tab]')];
const panels=[...document.querySelectorAll('[data-app-panel]')];
const aliases={matchPicker:'matches',results:'matches',matchResearch:'matches',calculator:'matches',
  predictionHistory:'history',automationForecasts:'history',qualityDashboard:'quality',qualityNightly:'quality',trainingStatus:'quality',backtesting:'quality',serviceStatus:'data'};
let active=null;
const availableTabs=()=>tabs.filter(tab=>!tab.classList.contains('owner-only')||document.body.classList.contains('owner-mode'));
const ownerFragments=new Set(['data','serviceStatus','calculator','backtesting','trainingStatus','qualityNightly','automationForecasts']);

function selectFromHash({scroll=false}={}){
  let fragment='';try{fragment=decodeURIComponent(location.hash.slice(1));}catch{/* Invalid fragments open the default view. */}
  const key=aliases[fragment]||fragment;
  if(ownerFragments.has(fragment))document.body.classList.add('owner-mode');
  const next=availableTabs().some(tab=>tab.dataset.appTab===key)?key:'matches';
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
    const visible=availableTabs(),index=visible.indexOf(tab);
    const next=event.key==='ArrowRight'?(index+1)%visible.length:event.key==='ArrowLeft'?(index+visible.length-1)%visible.length:event.key==='Home'?0:event.key==='End'?visible.length-1:null;
    if(next===null)return;event.preventDefault();visible[next].focus();visible[next].click();
  });
}
window.addEventListener('hashchange',()=>selectFromHash({scroll:true}));
window.addEventListener('popstate',()=>selectFromHash({scroll:true}));
document.getElementById('exitOwnerMode')?.addEventListener('click',event=>{
  event.preventDefault();document.body.classList.remove('owner-mode');
  const token=document.getElementById('ownerToken');if(token)token.value='';
  history.pushState(null,'','#matches');selectFromHash({scroll:true});
  window.dispatchEvent(new CustomEvent('footpredict:public-mode'));
});
selectFromHash();
