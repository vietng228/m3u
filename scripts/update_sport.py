#!/usr/bin/env python3
import html,json,re,time
from pathlib import Path
from urllib.parse import urljoin,urlparse
from datetime import datetime,timezone,timedelta
import requests
from bs4 import BeautifulSoup

UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151 Safari/537.36'
VN=timezone(timedelta(hours=7)); WINDOW=120
SOURCES=[('XOILAC','https://xoilacxbl.tv/'),('BIAOMTV','https://biaomtv.pro/')]
MEDIA=re.compile(r'https?://[^\"\'<>\\\s]+?\.(?:m3u8|flv|mpd)(?:\?[^\"\'<>\\\s]*)?',re.I)

def clean(s):return html.unescape(s or '').replace('\\/','/').replace('\\u0026','&').strip().rstrip('\\')
def txt(s):return re.sub(r'\s+',' ',html.unescape(s or '')).strip()
def minute(s):
 m=re.search(r'(?<!\d)([01]?\d|2[0-3])[:h]([0-5]\d)(?!\d)',s or '',re.I)
 return int(m.group(1))*60+int(m.group(2)) if m else 99999
def live_text(s):
 b=(s or '').casefold()
 if any(x in b for x in ('kết thúc','full time',' ft ','hoãn','hủy')):return False
 return any(x in b for x in ('🔴',' live','live ','đang diễn ra','đang đá','hiệp 1','hiệp 2','half time',' ht ','penalty'))
def in_window(label):
 if live_text(label):return True
 m=minute(label)
 if m==99999:return False
 now=datetime.now(VN);cur=now.hour*60+now.minute
 d=(m-cur)%1440
 return 0<=d<=WINDOW
def ses(ref):
 s=requests.Session();s.headers.update({'User-Agent':UA,'Referer':ref,'Accept-Language':'vi-VN,vi;q=0.9,en;q=0.7'});return s
def get(s,u,ref=None):
 r=s.get(u,headers={'Referer':ref} if ref else {},timeout=15,allow_redirects=True);r.raise_for_status();r.encoding=r.apparent_encoding or 'utf-8';return r.text,r.url
def medias(t):
 out=[]
 for u in MEDIA.findall(t or ''):
  u=clean(u)
  if u not in out:out.append(u)
 return out

def discover(label,home):
 s=ses(home);text,final=get(s,home);soup=BeautifulSoup(text,'html.parser');host=urlparse(final).netloc;out={}
 for a in soup.find_all('a',href=True):
  u=urljoin(final,a['href']);p=urlparse(u);path=p.path.lower();name=txt(' '.join(a.stripped_strings))
  if label=='XOILAC':
   if p.netloc!=host or ('/truc-tiep/' not in path and '/live/' not in path):continue
  else:
   # Biaom có thể đổi domain/path; giữ card có giờ hoặc trạng thái LIVE, bỏ link điều hướng rác.
   if not name or not (live_text(name) or minute(name)!=99999):continue
   if p.scheme not in ('http','https'):continue
  if not name:name=p.path.rstrip('/').split('/')[-1].replace('-',' ')
  out[u]=name[:220]
 matches=[(n,u) for u,n in out.items() if in_window(n)]
 print(f'[{label}] discovered={len(out)} selected_live_or_next_2h={len(matches)}')
 return s,final,matches

def page_meta(text,fallback):
 soup=BeautifulSoup(text,'html.parser');title=''
 x=soup.find('meta',attrs={'property':'og:title'}) or soup.find('meta',attrs={'name':'twitter:title'})
 if x and x.get('content'):title=txt(x['content'])
 if not title:title=txt(soup.title.string if soup.title and soup.title.string else fallback)
 body=txt(soup.get_text(' ',strip=True))[:5000];blob=title+' '+body
 m=minute(blob);is_live=live_text(blob)
 name=re.sub(r'^Link\s+trực\s+tiếp\s+','',title,flags=re.I)
 name=re.sub(r'\s+\d{1,2}:\d{2},?\s*ngày.*$','',name,flags=re.I)
 name=re.sub(r'\s*[-|]\s*(?:Xoilac|Biaom).*$','',name,flags=re.I)
 return txt(name) or fallback,m,is_live

def xoilac(s,url,fallback):
 text,page=get(s,url);name,m,is_live=page_meta(text,fallback);d=medias(text)
 if d:return d[0],page,name,m,is_live
 z=re.search(r'\blist_stream\s*=\s*(\[\[.*?\]\])\s*;',text,re.S);eps=[]
 if z:
  raw=z.group(1).replace('\\/','/')
  try:
   for g in json.loads(raw):
    if isinstance(g,list):eps.extend(x for x in g if isinstance(x,str))
  except:eps.extend(re.findall(r'https?://[^\"\'\s\]]+/ajax/chanel/[^\"\'\s\]]+',raw,re.I))
 for ep in eps:
  for pu in (ep.rstrip('/')+'/off-tvc?is_off_add=false',ep):
   try:t,ref=get(s,pu,page)
   except:continue
   q=re.search(r'\burlStream\s*=\s*[\"\']([^\"\']+)[\"\']',t,re.I);d=medias(t)
   if q:return clean(q.group(1)),ref,name,m,is_live
   if d:return d[0],ref,name,m,is_live
 return None,page,name,m,is_live

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
   p.goto(url,wait_until='domcontentloaded',timeout=12000);end=time.time()+8
   while time.time()<end and not hits:p.wait_for_timeout(250)
   title=txt(p.title()) or fallback
   try:body=txt(p.locator('body').inner_text(timeout=1500))[:5000]
   except:body=''
   blob=title+' '+body;m=minute(blob);is_live=live_text(blob)
   name=re.sub(r'\s*[-|]\s*Biaom.*$','',title,flags=re.I).strip() or fallback
   hits.sort(key=lambda u:0 if '.m3u8' in u.lower() else 1 if '.flv' in u.lower() else 2)
   return (hits[0] if hits else None),p.url,name,m,is_live
  finally:p.close()
 def close(self):
  for o,m in ((self.c,'close'),(self.b,'close'),(self.pw,'stop')):
   try:
    if o:getattr(o,m)()
   except:pass

def fmt(name,m,is_live):
 name=txt(name).replace(' vs ',' - ').replace(' VS ',' - ');pre='🔴 LIVE • ' if is_live else ''
 return pre+name if m==99999 else f'{pre}{m//60:02d}:{m%60:02d} • {name}'

def main():
 rows=[];bb=None
 try:
  for label,home in SOURCES:
   try:s,final,matches=discover(label,home)
   except Exception as e:print(label,'home error',e);continue
   if label=='BIAOMTV' and matches:bb=Browser();bb.start()
   for fallback,url in matches:
    try:stream,ref,name,m,is_live=xoilac(s,url,fallback) if label=='XOILAC' else bb.resolve(url,fallback)
    except Exception as e:print(label,fallback,'error',e);continue
    # Kiểm tra lần cuối sau khi đọc metadata trang.
    if stream and (is_live or in_window(f'{m//60:02d}:{m%60:02d}' if m!=99999 else '')):
     rows.append((0 if is_live else 1,m,label,fmt(name,m,is_live),stream,ref or final))
  rows.sort(key=lambda x:(x[0],x[1],x[3].casefold()))
  now=datetime.now(VN).strftime('%d/%m/%Y %H:%M:%S GMT+7');lines=['#EXTM3U','# Cập nhật: '+now]
  for _,_,group,name,stream,ref in rows:
   lines += [f'#EXTINF:-1 group-title="{group}",{name.replace(chr(34),chr(39))}','#EXTVLCOPT:http-user-agent='+UA,'#EXTVLCOPT:http-referrer='+ref,stream]
  Path('sport.m3u').write_text('\n'.join(lines)+'\n',encoding='utf-8-sig')
  print('sources:',len(rows),'live:',sum(1 for r in rows if r[0]==0),'updated',now)
 finally:
  if bb:bb.close()
if __name__=='__main__':main()
