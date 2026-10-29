export function cleanText(value,maximum=600) {
  return String(value??'').replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g,'$1').replace(/<[^>]*>/g,' ')
    .replace(/&(?:amp|lt|gt|quot|apos|nbsp|rsquo|lsquo|rdquo|ldquo|ndash|mdash|#\d+|#x[0-9a-f]+);/gi,entity=>{
      const known={'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&apos;':"'",'&nbsp;':' ','&rsquo;':'’','&lsquo;':'‘','&rdquo;':'”','&ldquo;':'“','&ndash;':'–','&mdash;':'—'};
      if(known[entity.toLowerCase()])return known[entity.toLowerCase()];
      const code=entity[2].toLowerCase()==='x'?parseInt(entity.slice(3,-1),16):parseInt(entity.slice(2,-1),10);
      return Number.isInteger(code)&&code>0&&code<=0x10ffff?String.fromCodePoint(code):'';
    }).replace(/\s+/g,' ').trim().slice(0,maximum);
}
