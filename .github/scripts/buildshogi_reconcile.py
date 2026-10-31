"""Local-only proposal: a read-only witness for two existing blocked Issues."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode,urlsplit,parse_qs
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler

REPO="kokiSAT0/kokisat0.github.io"
API="https://api.github.com/repos/"+REPO
WORKFLOW=".github/workflows/buildshogi-journal-reconcile.yml"
SCRIPT=".github/scripts/buildshogi_reconcile.py"
BINDING=".github/buildshogi-reconcile-binding.json"
GROUP="buildshogi-journal-reconcile"
OLD_WORKFLOW=".github/workflows/buildshogi-ops-monitor.yml"
BASELINE=41179058909
FAILED=41181823750
OLD_HEAD="30ebd9980817f2961064e7ae4629bfe75b28066a"
BASE_HEAD="c99b8fa38f6c7303913a272c4b36e0c1e7af2c05"
STATE={"schema":2,"sequence":0,"highWater":"2026-10-31T07:45:11Z","previousStateSHA256":None,
       "lastRunID":FAILED,"lastRunNumber":2,"lastRunHeadSHA":OLD_HEAD,"status":"blocked"}
ALERT={"schema":2,"status":"blocked","reason":"coverage-unverified"}
PINNED={
 1:("[BuildShogi] operations observer state","key: buildshogi-ops-observer-state/v2\n",STATE),
 2:("[BuildShogi] operations health alert","key: buildshogi-ops-health-alert/v2\n",ALERT)}
PREFIXES=("key: buildshogi-ops-observer-state/","key: buildshogi-ops-health-alert/")
CLEANUP={OLD_WORKFLOW:"c2bad4f56f93ece1e8d99b13da87aa42bc59eea31bb0586d9613f7eac87b0045",
 ".github/buildshogi-monitor-binding.json":"70fd97898d0d9e2b56251306da9ab7f8353c0213d96c08cdc09ecb96a2c8cd87"}
STAGES={"context","current-run","baseline-run","failed-run","issues-first","issues-second","complete"}
REASONS={"context-invalid","run-conflict","issue-conflict","list-conflict","list-incomplete",
 "pagination-invalid","budget","read-unconfirmed","binding-invalid","source-conflict",
 "clock-conflict","approval-required","duplicate-key"}

def fail(code):raise ValueError(code)
def encoded(v):return json.dumps(v,sort_keys=True,separators=(",",":"),allow_nan=False)
def digest(v):return hashlib.sha256(encoded(v).encode()).hexdigest()
def positive(v):return type(v) is int and 0<v<2**53
def strict(raw):
 def pairs(items):
  result={}
  for key,value in items:
   if key in result:fail("duplicate-key")
   result[key]=value
  return result
 return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail("read-unconfirmed"))
def stamp(v):
 if not isinstance(v,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",v):fail("clock-conflict")
 return datetime.strptime(v,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
def binding(v):
 if (not isinstance(v,dict) or set(v)!={"schema","mode","repository","expectedWitnessRunNumber","scriptSHA256","workflowSHA256"}
     or type(v["schema"]) is not int or v["schema"]!=1 or v["mode"]!="read-only-witness"
     or v["repository"]!=REPO or type(v["expectedWitnessRunNumber"]) is not int or v["expectedWitnessRunNumber"]!=1
     or any(not isinstance(v[k],str) or not re.fullmatch(r"[a-f0-9]{64}",v[k]) for k in ("scriptSHA256","workflowSHA256"))):
  fail("binding-invalid")
 return v
def local_binding(root):
 root=Path(root)
 def read(relative):
  parts=Path(relative).parts
  if any(p.is_symlink() for p in [root,*[root.joinpath(*parts[:i]) for i in range(1,len(parts)+1)]]):fail("source-conflict")
  p=root/relative
  if not p.is_file() or p.stat().st_size>65536:fail("source-conflict")
  return p.read_bytes()
 v=binding(strict(read(BINDING)))
 expected={SCRIPT:v["scriptSHA256"],WORKFLOW:v["workflowSHA256"],**CLEANUP}
 for relative,want in expected.items():
  if hashlib.sha256(read(relative)).hexdigest()!=want:fail("source-conflict")
 return v
def context(v):
 if (not isinstance(v,dict) or set(v)!={"repository","runID","headSHA","attempt","ref","concurrency"}
     or v["repository"]!=REPO or not positive(v["runID"]) or v["runID"] in (BASELINE,FAILED)
     or not isinstance(v["headSHA"],str) or not re.fullmatch(r"[a-f0-9]{40}",v["headSHA"])
     or type(v["attempt"]) is not int or v["attempt"]!=1 or v["ref"]!="refs/heads/main" or v["concurrency"]!=GROUP):
  fail("context-invalid")
 return v
def run(v,*,id,number,head,path,current=False,conclusion="success",workflow_id=None):
 if (not isinstance(v,dict) or not positive(v.get("id")) or v["id"]!=id or not positive(v.get("run_number")) or v["run_number"]!=number
     or type(v.get("run_attempt")) is not int or v["run_attempt"]!=1 or v.get("head_sha")!=head
     or v.get("head_branch")!="main" or v.get("path")!=path or v.get("event")!="workflow_dispatch"
     or not positive(v.get("workflow_id")) or workflow_id is not None and v["workflow_id"]!=workflow_id
     or not isinstance(v.get("repository"),dict) or v["repository"].get("full_name")!=REPO
     or (current and (v.get("status")!="in_progress" or v.get("conclusion") is not None))
     or (not current and (v.get("status")!="completed" or v.get("conclusion")!=conclusion))):
  fail("run-conflict")
 return v
def issue(v,number):
 title,marker,value=PINNED[number]
 if (not isinstance(v,dict) or type(v.get("number")) is not int or v["number"]!=number
     or v.get("title")!=title or v.get("state")!="open" or "pull_request" in v
     or not isinstance(v.get("user"),dict) or v["user"].get("login")!="github-actions[bot]" or v["user"].get("type")!="Bot"
     or v.get("body")!=marker+encoded(value) or v.get("created_at")!="2026-10-31T07:45:12Z"
     or v.get("updated_at")!="2026-10-31T07:45:12Z"):
  fail("issue-conflict")
 return value
def page_url(n):
 if type(n) is not int or not 1<=n<=10:fail("pagination-invalid")
 return API+"/issues?"+urlencode({"state":"all","sort":"created","direction":"asc","per_page":100,"page":n})
def next_page(link,page):
 if not isinstance(link,str) or len(link)>4096:fail("pagination-invalid")
 result=None
 for part in link.split(",") if link else []:
  match=re.fullmatch(r'\s*<([^>]+)>;\s*rel="(next|prev|last|first)"\s*',part)
  if not match:fail("pagination-invalid")
  u=urlsplit(match[1]);q=parse_qs(u.query,keep_blank_values=True)
  try:n=int(q["page"][0])
  except (KeyError,ValueError,IndexError):fail("pagination-invalid")
  if (u.scheme!="https" or u.netloc!="api.github.com" or u.path!="/repos/"+REPO+"/issues" or u.fragment
      or q!=parse_qs(urlsplit(page_url(n)).query)):fail("pagination-invalid")
  if match[2]=="next":
   if result is not None or n!=page+1:fail("pagination-invalid")
   result=n
 return result
def scan(http):
 page=1;seen=set()
 for _ in range(10):
  rows=http.get(page_url(page))
  if not isinstance(rows,list) or len(rows)>100:fail("list-conflict")
  for row in rows:
   if not isinstance(row,dict):fail("list-conflict")
   if row.get("title") in {p[0] for p in PINNED.values()} or isinstance(row.get("body"),str) and row["body"].startswith(PREFIXES):
    number=row.get("number")
    if type(number) is not int or number not in PINNED or number in seen:fail("list-conflict")
    issue(row,number);seen.add(number)
  n=next_page(http.last_link,page)
  if n is None:
   if seen!=set(PINNED):fail("list-incomplete")
   return
  page=n
 fail("list-incomplete")
class HTTP:
 class NoRedirect(HTTPRedirectHandler):
  def redirect_request(self,*a,**k):return None
 def __init__(self,current_id):
  if not positive(current_id) or current_id in (BASELINE,FAILED):fail("context-invalid")
  self.allowed={API+"/actions/runs/"+str(n) for n in (current_id,BASELINE,FAILED)}
  self.allowed|={API+"/issues/"+str(n) for n in PINNED}
  self.allowed|={page_url(n) for n in range(1,11)}
  self.opener=build_opener(ProxyHandler({}),self.NoRedirect())
  self.calls=0;self.until=time.monotonic()+145;self.last_link=""
 def get(self,url):
  if url not in self.allowed:fail("read-unconfirmed")
  if self.calls>=32 or time.monotonic()>=self.until:fail("budget")
  self.calls+=1
  try:
   request=Request(url,method="GET",headers={"Accept":"application/vnd.github+json","User-Agent":"BuildShogiReconcile","Cache-Control":"no-cache","X-GitHub-Api-Version":"2022-11-28"})
   with self.opener.open(request,timeout=min(10,self.until-time.monotonic())) as response:
    raw=response.read(524289)
    if response.geturl()!=url or response.status!=200 or len(raw)>524288 or time.monotonic()>=self.until:fail("read-unconfirmed")
    self.last_link=response.headers.get("Link","")
    if len(self.last_link)>4096:fail("read-unconfirmed")
    return strict(raw)
  except Exception:fail("read-unconfirmed")
def reconcile(b,c,http,now,*,progress=None):
 binding(b);context(c);stamp(now)
 def mark(stage):
  if stage not in STAGES:fail("context-invalid")
  if progress is not None:progress({"event":"readonly-reconciliation","stage":stage})
 mark("context");mark("current-run")
 current=run(http.get(API+"/actions/runs/"+str(c["runID"])),id=c["runID"],number=1,head=c["headSHA"],path=WORKFLOW,current=True)
 if current["workflow_id"]==402524099:fail("run-conflict")
 mark("baseline-run")
 run(http.get(API+"/actions/runs/"+str(BASELINE)),id=BASELINE,number=1,head=BASE_HEAD,path=OLD_WORKFLOW,workflow_id=402524099)
 mark("failed-run")
 failed=run(http.get(API+"/actions/runs/"+str(FAILED)),id=FAILED,number=2,head=OLD_HEAD,path=OLD_WORKFLOW,conclusion="failure",workflow_id=402524099)
 if stamp(now)<stamp(failed.get("updated_at")) or stamp(now)<stamp(STATE["highWater"]):fail("clock-conflict")
 for stage in ("issues-first","issues-second"):
  mark(stage)
  for number in PINNED:issue(http.get(API+"/issues/"+str(number)),number)
  scan(http)
 mark("complete")
 return {"schema":1,"status":"blocked","witnessRunID":current["id"],"witnessWorkflowID":current["workflow_id"],
         "witnessHeadSHA":current["head_sha"],"verifiedAt":now,"failedBootstrapRunID":FAILED,
         "failedBootstrapRemainsFailure":True,"stateIssue":1,"alertIssue":2,"stateSHA256":digest(STATE),
         "alertSHA256":digest(ALERT),"existingPayloadsConfirmed":True,"monitoringEnabled":False,
         "observerRecoveryAuthorized":False,"collectionEnabled":False,"issueWrites":0}
def failure_result(exc):
 reason=str(exc) if type(exc) is ValueError and str(exc) in REASONS else "unclassified-error"
 return {"status":"blocked","outcome":"readonly-witness-unconfirmed","reason":reason,"issueWrites":0}
def main():
 parser=argparse.ArgumentParser();parser.add_argument("--execute-approved-read-only",action="store_true")
 args=parser.parse_args()
 if not args.execute_approved_read_only:
  print('{"executed":false,"status":"inactive-read-only-draft"}');return
 if os.environ.get("BUILDSHOGI_RECONCILIATION_APPROVED")!="true":fail("approval-required")
 b=local_binding(Path(__file__).resolve().parents[2])
 c=context({"repository":os.environ.get("GITHUB_REPOSITORY"),"runID":int(os.environ.get("GITHUB_RUN_ID","0")),
  "headSHA":os.environ.get("GITHUB_SHA"),"attempt":int(os.environ.get("GITHUB_RUN_ATTEMPT","0")),
  "ref":os.environ.get("GITHUB_REF"),"concurrency":os.environ.get("BUILDSHOGI_CONCURRENCY")})
 result=reconcile(b,c,HTTP(c["runID"]),datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  progress=lambda v:print(encoded(v),flush=True))
 print(encoded(result))
if __name__=="__main__":
 try:main()
 except Exception as exc:
  print(encoded(failure_result(exc)),flush=True);raise SystemExit(1)
