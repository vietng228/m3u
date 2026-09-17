#!/usr/bin/env python3
import html,json,re,time
from pathlib import Path
from urllib.parse import urljoin,urlparse
from datetime import datetime,timezone,timedelta
import requests
from bs4 import BeautifulSoup

UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151 Safari/537.36'
VN=timezone(timedelta(hours=7))
SOURCES=[('XOILAC','https://xoilacxbl.tv/'),('BIAOMTV','https://biaomtv.pro/')]
MEDIA=re.compile(r'https?://[^\"\'<>\\\s]+?\.(?:m3u8|flv|mpd)(?:\?[^\"\'<>\\\s]*)?',re.I)

def clean(s):return html.unescape(s or '').replace('\\/','/').replace('\\u0026','&').strip().rstrip('\\')
def text_clean(s):return re.sub(r'\s+',' ',html.unescape(s or '')).strip()
def sess(ref):
 s=requests.Session();s.headers.update({'User-Agent':UA,'Referer':ref,'Accept-Language':'vi-VN,vi;q=0.9,en;q=0.7'});return s
def get(s,u,ref=None):
 r=s.get(u,headers={'Referer':ref} if ref else {},timeout=18,allow_redirects=True);r.raise_for_status();r.encoding=r.apparent_encoding or 'utf-8';return r.text,r.url
def medias(t):
 out=[]
 for u in MEDIA.findall(t or ''):
  u=clean(u)
  if u not in out:out.append(u)
 return out

def parse_minute(blob):
 tm=re.search(r'(?<!\d)([01]?\d|2[0-3])[:h]([0-5]\d)(?!\d)',blob or '',re.I)
 if not tm:tm=re.search(r'(?:luc|lúc)[-_ ]?([01]?\d|2[0-3])[-_:h]?([0-5]\d)',blob or '',re.I)
 return int(tm.group(1))*60+int(tm.group(2)) if tm else 99999

def detect_live(blob,minute):
 b=(blob or '').casefold()
 # Ưu tiên trạng thái live thật từ nguồn nếu có.
 live_words=('đang diễn ra','đang đá','trực tiếp','live now','đang live','hiệp 1','hiệp 2','half time',' ht ','penalty')
 end_words=('kết thúc','đã kết thúc','full time',' ft ','hoãn','hủy','chưa bắt đầu')
 if any(x in b for x in end_words):return False
 if any(x in b for x in live_words):return True
 # Fallback khi nguồn không có status: coi trận trong cửa sổ từ giờ bắt đầu đến +150 phút là LIVE.
 if minute!=99999:
  now=datetime.now(VN);cur=now.hour*60+now.minute
  delta=cur-minute
  if delta<0:delta+=1440
  return 0<=delta<=150
 return False

def meta_from_page(text,url,fallback):
 soup=BeautifulSoup(text,'html.parser');title=''
 for key,attr in [('property','og:title'),('name','twitter:title')]:
  x=soup.find('meta',attrs={key:attr})
  if x and x.get('content'):title=text_clean(x['content']);break
 if not title:title=text_clean(soup.title.string if soup.title and soup.title.string else fallback)
 desc='';x=soup.find('meta',attrs={'name':'description'}) or soup.find('meta',attrs={'property':'og:description'})
 if x and x.get('content'):desc=text_clean(x['content'])
 body=''
 try:body=text_clean(soup.get_text(' ',strip=True))[:6000]
 except:pass
 blob=' '.join([title,desc,body,url]);minute=parse_minute(' '.join([title,desc,url]))
 name=title
 name=re.sub(r'^Link\s+trực\s+tiếp\s+','',name,flags=re.I)
 name=re.sub(r'\s+\d{1,2}:\d{2},?\s*ngày.*$','',name,flags=re.I)
 name=re.sub(r'\s*-\s*Xoilac.*$','',name,flags=re.I)
 name=re.sub(r'\s+(?:lúc|luc)\s+\d{1,2}:?\d{2}.*$','',name,flags=re.I)
 name=text_clean(name) or text_clean(fallback)
 return name,minute,detect_live(blob,minute)

def discover(label,home):
 s=sess(home);text,final=get(s,home);soup=BeautifulSoup(text,'html.parser');host=urlparse(final).netloc;out={}
 for a in soup.find_all('a',href=True):
  u=urljoin(final,a['href']);p=urlparse(u);path=p.path.lower()
  if p.netloc!=host or ('/truc-tiep/' not in path and '/live/' not in path):continue
  fallback=text_clean(' '.join(a.stripped_strings)) or p.path.rstrip('/').split('/')[-1].replace('-',' ')
  out[u]=fallback[:180]
 return s,final,[(n,u) for u,n in out.items()]

def xoilac(s,url,fallback):
 text,page=get(s,url);name,minute,is_live=meta_from_page(text,page,fallback);d=medias(text)
 if d:return d[0],page,name,minute,is_live
 m=re.search(r'\blist_stream\s*=\s*(\[\[.*?\]\])\s*;',text,re.S);eps=[]
 if m:
  raw=m.group(1).replace('\\/','/')
  try:
   for g in json.loads(raw):
    if isinstance(g,list):eps.extend(x for x in g if isinstance(x,str))
  except:eps.extend(re.findall(r'https?://[^\"\'\s\]]+/ajax/chanel/[^\"\'\s\]]+',raw,re.I))
 for ep in eps:
  for pu in (ep.rstrip('/')+'/off-tvc?is_off_add=false',ep):
   try:t,final=get(s,pu,page)
   except:continue
   m2=re.search(r'\burlStream\s*=\s*[\"\']([^\"\']+)[\"\']',t,re.I)
   if m2:return clean(m2.group(1)),final,name,minute,is_live
   d=medias(t)
   if d:return d[0],final,name,minute,is_live
 return None,page,name,minute,is_live

class Browser:
 def __init__(self):self.pw=self.b=self.c=None
 def start(self):
  from playwright.sync_api import sync_playwright
  self.pw=sync_playwright().start();self.b=self.pw.chromium.launch(headless=True,args=['--autoplay-policy=no-user-gesture-required']);self.c=self.b.new_context(user_agent=UA,locale='vi-VN',timezone_id='Asia/Ho_Chi_Minh')
 def resolve(self,url,fallback):
  if not self.c:self.start()
  p=self.c.new_page();hits=[]
  p.on('request',lambda r:hits.append(r.url) if any(x in r.url.lower() for x in ('.m3u8','.flv','.mpd')) and r.url not in hits else None)
  try:
   p.goto(url,wait_until='domcontentloaded',timeout=18000);end=time.time()+16
   while time.time()<end and not hits:p.wait_for_timeout(500)
   title=text_clean(p.title()) or fallback;body=''
   try:body=text_clean(p.locator('body').inner_text(timeout=2000))[:6000]
   except:pass
   blob=title+' '+body+' '+url;minute=parse_minute(blob)
   name=re.sub(r'\s*[-|]\s*Biaom.*$','',title,flags=re.I).strip() or fallback
   is_live=detect_live(blob,minute)
   hits.sort(key=lambda u:0 if '.m3u8' in u.lower() else 1 if '.flv' in u.lower() else 2)
   return (hits[0] if hits else None),p.url,name,minute,is_live
  finally:p.close()
 def close(self):
  for o,m in ((self.c,'close'),(self.b,'close'),(self.pw,'stop')):
   try:
    if o:getattr(o,m)()
   except:pass

def fmt_name(name,minute,is_live):
 name=text_clean(name).replace(' vs ',' - ').replace(' VS ',' - ')
 prefix='🔴 LIVE • ' if is_live else ''
 if minute==99999:return prefix+name
 return f'{prefix}{minute//60:02d}:{minute%60:02d} • {name}'

def main():
 rows=[];bb=None
 try:
  for label,home in SOURCES:
   try:s,final,matches=discover(label,home)
   except Exception as e:print(label,'home error',e);continue
   print(label,'matches',len(matches))
   if label=='BIAOMTV' and matches:bb=Browser();bb.start()
   for fallback,url in matches:
    try:stream,ref,name,minute,is_live=xoilac(s,url,fallback) if label=='XOILAC' else bb.resolve(url,fallback)
    except Exception as e:print(label,fallback,'error',e);continue
    if stream:rows.append((0 if is_live else 1,minute,label,fmt_name(name,minute,is_live),stream,ref or final))
  # LIVE luôn nằm trên cùng, sau đó mới sắp theo giờ.
  rows.sort(key=lambda x:(x[0],x[1],x[3].casefold()))
  now=datetime.now(VN).strftime('%d/%m/%Y %H:%M:%S GMT+7');lines=['#EXTM3U','# Cập nhật: '+now]
  for _,_,group,name,stream,ref in rows:
   safe=name.replace('"',"'")
   lines += [f'#EXTINF:-1 group-title="{group}",{safe}','#EXTVLCOPT:http-user-agent='+UA,'#EXTVLCOPT:http-referrer='+ref,stream]
  Path('sport.m3u').write_text('\n'.join(lines)+'\n',encoding='utf-8-sig')
  print('sources:',len(rows),'live:',sum(1 for r in rows if r[0]==0),'updated',now)
 finally:
  if bb:bb.close()
if __name__=='__main__':main()
