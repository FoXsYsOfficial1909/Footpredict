// Conservative whole-message translation of structured player reports.
// Original source strings, dates, identities and prediction inputs stay intact.
export const SOURCE_TRANSLATION_VERSION='source-ru-1';

const CONDITIONS=new Map(Object.entries({
  'knee injury':'Травма колена',
  'hamstring injury':'Травма задней поверхности бедра',
  'ankle injury':'Травма голеностопа',
  'thigh injury':'Травма бедра',
  'hip injury':'Травма в области тазобедренного сустава',
  'calf injury':'Травма икроножной области',
  'groin injury':'Травма паховой области',
  'foot injury':'Травма стопы',
  'head injury':'Травма головы',
  'back injury':'Травма спины',
  'achilles injury':'Травма ахиллова сухожилия',
  'shin injury':'Травма передней поверхности голени',
  'muscular injury':'Мышечная травма',
  'unspecified injury':'Травма, характер не уточнён',
  'illness':'Заболевание',
  'knock':'Повреждение',
  'concussion':'Сотрясение мозга'
}));
const MONTHS=[['jan','january','января',31],['feb','february','февраля',29],
  ['mar','march','марта',31],['apr','april','апреля',30],['may','may','мая',31],
  ['jun','june','июня',30],['jul','july','июля',31],['aug','august','августа',31],
  ['sep','september','сентября',30],['oct','october','октября',31],
  ['nov','november','ноября',30],['dec','december','декабря',31]];

function dateRu(value){
  const match=value.match(/^(\d{1,2}) (\p{L}+)$/u);
  if(!match)return null;
  const month=MONTHS.find(([short,long])=>[short,long].includes(match[2].toLowerCase()));
  const day=Number(match[1]);
  return month&&day>=1&&day<=month[3]?day+' '+month[2]:null;
}

function translatedMessage(value,{allowFplChance}){
  const suspended=value.match(/^Suspended until (.+)$/i);
  if(suspended){const date=dateRu(suspended[1]);return date?'Дисквалификация до '+date:null;}
  if(/^not included in squad\.?$/i.test(value))return 'Не включён в состав команды.';
  const pieces=value.split(' - ');
  if(pieces.length!==2)return null;
  const condition=CONDITIONS.get(pieces[0].toLowerCase());
  if(!condition)return null;
  const detail=pieces[1];
  if(/^Unknown return date$/i.test(detail))return condition+'. Срок возвращения неизвестен.';
  const expected=detail.match(/^Expected back (.+)$/i);
  if(expected){const date=dateRu(expected[1]);return date?condition+'. Ожидаемое возвращение — '+date+'.':null;}
  const chance=detail.match(/^(25|50|75)% chance of playing$/i);
  if(chance&&allowFplChance)return condition+'. Шанс сыграть по оценке FPL — '+chance[1]+'%.';
  return null;
}

/** No network or AI calls. A partial/unknown translation is never presented as complete. */
export function translateSourceText(value,{kind='availability'}={}){
  const original=typeof value==='string'?value:'';
  const base={status:'unavailable',text:null,original,language:null,method:null,version:SOURCE_TRANSLATION_VERSION};
  if(!original.trim())return {...base,status:'empty'};
  if(original.length>600||!['availability','news'].includes(kind)||/[\u0000-\u001f\u007f<>]/.test(original))return base;
  if(/\p{Script=Cyrillic}/u.test(original)&&!/[a-z]/i.test(original))
    return {...base,status:'already_russian',text:original,language:'ru'};
  // Normalise spacing/case for matching only; the original remains byte-for-byte.
  const normalized=original.trim().replace(/\s+/g,' ').replace(/ [–—] /g,' - ');
  const text=translatedMessage(normalized,{allowFplChance:kind==='availability'});
  return text?{...base,status:'translated',text,language:'ru',method:'glossary'}:base;
}
