#!/usr/bin/env python3
import html,json,re,time
from pathlib import Path
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup

UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/151 Safari/537.36'
SOURCES=[('XOILAC','https://xoilacxbl.tv/'),('BIAOMTV','https://biaomtv.pro/')]
MEDIA=re.compile(r'https?://[^\"\'<>\\\s]+?\.(?:m3u8|flv|mpd)(?:\?[^\"\'<>\\\s]*)?',re.I)

def clean(s): return html.unescape(s).replace('\\/','/').replace('\\u0026','&').strip().rstrip('\\')
def sess(ref):
 s=requests.Session(); s.headers.update({'User-Agent':UA,'Referer':ref}); return s
def get(s,u,ref=None):
 r=s.get(u,headers={'Referer':ref} if ref else {},timeout=18,allow_redirects=True); r.raise_for_status(); return r.text,r.url
def medias(t):
 out=[]
 for u in MEDIA.findall(t or ''):
  u=clean(u)
  if u not in out: out.append(u)
 return out

def discover(label,home):
 s=sess(home); text,final=get(s,home); soup=BeautifulSoup(text,'html.parser'); host=urlparse(final).netloc; out={}
 for a in soup.find_all('a',href=True):
  u=urljoin(final,a['href']); p=urlparse(u); path=p.path.lower()
  if p.netloc!=host or ('/truc-tiep/' not in path and '/live/' not in path): continue
  name=re.sub(r'\s+',' ',' '.join(a.stripped_strings)).strip() or p.path.rstrip('/').split('/')[-1].replace('-',' ')
  out[u]=name[:180]
 return s,final,[(n,u) for u,n in out.items()]

def xoilac(s,url):
 text,page=get(s,url); d=medias(text)
 if d:return d[0],page
 m=re.search(r'\blist_stream\s*=\s*(\[\[.*?\]\])\s*;',text,re.S); eps=[]
 if m:
  raw=m.group(1).replace('\\/','/')
  try:
   for g in json.loads(raw):
    if isinstance(g,list): eps.extend(x for x in g if isinstance(x,str))
  except: eps.extend(re.findall(r'https?://[^\"\'\s\]]+/ajax/chanel/[^\"\'\s\]]+',raw,re.I))
 for ep in eps:
  for pu in (ep.rstrip('/')+'/off-tvc?is_off_add=false',ep):
   try:t,final=get(s,pu,page)
   except:continue
   m2=re.search(r'\burlStream\s*=\s*[\"\']([^\"\']+)[\"\']',t,re.I)
   if m2:return clean(m2.group(1)),final
   d=medias(t)
   if d:return d[0],final
 return None,page

class Browser:
 def __init__(self):self.pw=self.b=self.c=None
 def start(self):
  from playwright.sync_api import sync_playwright
  self.pw=sync_playwright().start(); self.b=self.pw.chromium.launch(headless=True,args=['--autoplay-policy=no-user-gesture-required']); self.c=self.b.new_context(user_agent=UA,locale='vi-VN')
 def resolve(self,url):
  if not self.c:self.start()
  p=self.c.new_page(); hits=[]
  p.on('request',lambda r: hits.append(r.url) if any(x in r.url.lower() for x in ('.m3u8','.flv','.mpd')) and r.url not in hits else None)
  try:
   p.goto(url,wait_until='domcontentloaded',timeout=18000); end=time.time()+16
   while time.time()<end and not hits:p.wait_for_timeout(500)
   hits.sort(key=lambda u:0 if '.m3u8' in u.lower() else 1 if '.flv' in u.lower() else 2)
   return (hits[0] if hits else None),p.url
  finally:p.close()
 def close(self):
  for o,m in ((self.c,'close'),(self.b,'close'),(self.pw,'stop')):
   try:
    if o:getattr(o,m)()
   except:pass

def clock(name):
 m=re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)',name or '')
 return int(m.group(1))*60+int(m.group(2)) if m else 99999

def main():
 rows=[]; bb=None
 try:
  for label,home in SOURCES:
   try:s,final,matches=discover(label,home)
   except Exception as e: print(label,'home error',e); continue
   print(label,'matches',len(matches))
   if label=='BIAOMTV' and matches:bb=Browser(); bb.start()
   for name,url in matches:
    try:stream,ref=xoilac(s,url) if label=='XOILAC' else bb.resolve(url)
    except Exception as e:print(label,name,'error',e);continue
    if stream:rows.append((clock(name),label,name,stream,ref or final))
  rows.sort(key=lambda x:(x[0],x[2].casefold()))
  lines=['#EXTM3U','# Generated: '+time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime())]
  for _,group,name,stream,ref in rows:
   name=re.sub(r'\s+',' ',name).replace('"',"'")
   lines += [f'#EXTINF:-1 group-title="{group}",{name}','#EXTVLCOPT:http-user-agent='+UA,'#EXTVLCOPT:http-referrer='+ref,stream]
  Path('sport.m3u').write_text('\n'.join(lines)+'\n',encoding='utf-8')
  print('sources:',len(rows))
 finally:
  if bb:bb.close()
if __name__=='__main__':main()
