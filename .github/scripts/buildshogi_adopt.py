"""One-shot adoption proposal. Default inert; no DB, scheduler, creation or retries."""
import argparse
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import time
from urllib.request import Request

REPO="kokiSAT0/kokisat0.github.io"
API="https://api.github.com/repos/"+REPO
WORKFLOW=".github/workflows/buildshogi-ops-monitor.yml"
SCRIPT=".github/scripts/buildshogi_adopt.py"
BINDING=".github/buildshogi-adopt-binding.json"
GROUP="buildshogi-operations-monitor"
WITNESS=41184998290
WITNESS_WORKFLOW=402583723
WITNESS_HEAD="8e1c9b33b01163634fa511e2fef09309826815f9"
MONITOR=402524099
HELPERS={
 ".github/scripts/buildshogi_reconcile.py":"907a5d6ff369856871edfb645a384427b7a641f7fbc35df3c8fcd3466c9d7d9a",
 ".github/workflows/buildshogi-journal-reconcile.yml":"6a03bd7ede5eae160264af9adfb5fd747bde8954d65f70a90feed58a2eff3608",
 ".github/buildshogi-reconcile-binding.json":"ca56542309dbcb0fe1671eecfaf1b741a453bb4fb9a66958c3c39674dde49cab",
 ".github/buildshogi-monitor-binding.json":"70fd97898d0d9e2b56251306da9ab7f8353c0213d96c08cdc09ecb96a2c8cd87"}
STATE_SHA="ac148d318b9b025f586aefed6c24065735a878f0445e309b28d4c10dca2779f3"
ALERT_SHA="8cf034a0d8dc579f1796a23456809590ca44c9efb158a168e275a67c4f6b39ae"
STAGES={"local","context","history","before","patch","readback","complete"}
REASONS={"approval-required","binding-invalid","source-conflict","context-invalid","run-conflict",
 "clock-conflict","issue-conflict","list-conflict","list-incomplete","pagination-invalid","read-unconfirmed",
 "scope-conflict","budget","token-invalid","write-outcome-unknown","readback-unconfirmed","duplicate-key"}

def fail(reason):raise ValueError(reason)
def encoded(v):return json.dumps(v,sort_keys=True,separators=(",",":"),allow_nan=False)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def local_binding(root):
 root=Path(root)
 def read(relative):
  parts=Path(relative).parts
  if any(p.is_symlink() for p in [root,*[root.joinpath(*parts[:i]) for i in range(1,len(parts)+1)]]):fail("source-conflict")
  p=root/relative
  if not p.is_file() or p.stat().st_size>65536:fail("source-conflict")
  return p.read_bytes()
 # Before importing a helper, verify its exact reviewed source and its associated witnesses.
 for relative,want in HELPERS.items():
  if sha(read(relative))!=want:fail("source-conflict")
 spec=importlib.util.spec_from_file_location("reviewed_buildshogi_witness",root/".github/scripts/buildshogi_reconcile.py")
 helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
 b=helper.strict(read(BINDING))
 fixed={"schema":1,"mode":"adopt-existing-blocked-journal","enabled":True,"workflowID":MONITOR,
        "expectedRunNumber":3,"stateIssue":1,"alertIssue":2,"witnessRunID":WITNESS,
        "witnessWorkflowID":WITNESS_WORKFLOW,"witnessHeadSHA":WITNESS_HEAD,
        "expectedStateSHA256":STATE_SHA,"expectedAlertSHA256":ALERT_SHA}
 if not isinstance(b,dict) or set(b)!=set(fixed)|{"scriptSHA256","workflowSHA256"}:fail("binding-invalid")
 if any(type(b[k]) is not type(v) or b[k]!=v for k,v in fixed.items()):fail("binding-invalid")
 for relative,key in [(SCRIPT,"scriptSHA256"),(WORKFLOW,"workflowSHA256")]:
  if not isinstance(b[key],str) or not re.fullmatch("[a-f0-9]{64}",b[key]) or sha(read(relative))!=b[key]:fail("source-conflict")
 if helper.digest(helper.STATE)!=STATE_SHA or helper.digest(helper.ALERT)!=ALERT_SHA:fail("source-conflict")
 return b,helper

def context(c):
 if (not isinstance(c,dict) or set(c)!={"repository","runID","headSHA","attempt","ref","concurrency"}
  or c["repository"]!=REPO or type(c["runID"]) is not int or not 0<c["runID"]<2**53
  or c["runID"] in {41179058909,41181823750,WITNESS}
  or not isinstance(c["headSHA"],str) or not re.fullmatch("[a-f0-9]{40}",c["headSHA"])
  or type(c["attempt"]) is not int or c["attempt"]!=1 or c["ref"]!="refs/heads/main"
  or c["concurrency"]!=GROUP):fail("context-invalid")
 return c

def proposal(r,c,http,now,*,progress=None):
 context(c);r.stamp(now)
 def mark(stage):
  if stage not in STAGES:fail("context-invalid")
  if progress is not None:progress({"event":"journal-adoption","stage":stage})
 mark("context")
 current=r.run(http.get(API+"/actions/runs/"+str(c["runID"])),id=c["runID"],number=3,
               head=c["headSHA"],path=WORKFLOW,current=True,workflow_id=MONITOR)
 mark("history")
 for id,number,head,path,wid,conclusion in [
  (r.BASELINE,1,r.BASE_HEAD,WORKFLOW,MONITOR,"success"),
  (r.FAILED,2,r.OLD_HEAD,WORKFLOW,MONITOR,"failure"),
  (WITNESS,1,WITNESS_HEAD,r.WORKFLOW,WITNESS_WORKFLOW,"success")]:
  value=r.run(http.get(API+"/actions/runs/"+str(id)),id=id,number=number,head=head,path=path,
              workflow_id=wid,conclusion=conclusion)
  if r.stamp(now)<r.stamp(value.get("updated_at")):fail("clock-conflict")
 if r.stamp(now)<r.stamp(r.STATE["highWater"]):fail("clock-conflict")
 mark("before")
 # Revalidate all targets and complete scans twice. No failed-history exception leaks to normal monitor.
 for _ in range(2):
  for number in (1,2):r.issue(http.get(API+"/issues/"+str(number)),number)
  r.scan(http)
 after={"schema":2,"sequence":1,"highWater":now,"previousStateSHA256":r.digest(r.STATE),
        "lastRunID":current["id"],"lastRunNumber":3,"lastRunHeadSHA":current["head_sha"],"status":"blocked"}
 return {"after":after,"request":{"body":r.PINNED[1][1]+r.encoded(after)},
         "witnessRunID":WITNESS,"failedBootstrapRunID":r.FAILED,"previousStateSHA256":STATE_SHA,
         "afterStateSHA256":r.digest(after)}

def readback(r,row,after):
 title,marker,_=r.PINNED[1]
 if (not isinstance(row,dict) or type(row.get("number")) is not int or row["number"]!=1
     or row.get("title")!=title or row.get("state")!="open" or "pull_request" in row
     or not isinstance(row.get("user"),dict) or row["user"].get("login")!="github-actions[bot]"
     or row["user"].get("type")!="Bot" or row.get("created_at")!="2026-10-31T07:45:12Z"
     or row.get("body")!=marker+r.encoded(after)):fail("readback-unconfirmed")
 if r.stamp(row.get("updated_at"))<r.stamp(row["created_at"]):fail("readback-unconfirmed")

class HTTP:
 def __init__(self,r,c,*,token):
  context(c);self.r=r;self.c=c;self.reader=r.HTTP(c["runID"])
  self.reader.allowed.add(API+"/actions/runs/"+str(WITNESS))
  self.last_link="";self.token=token;self.expected=None;self.attempted=False
 def get(self,url):
  value=self.reader.get(url);self.last_link=self.reader.last_link;return value
 def arm(self,body):
  if self.expected is not None or self.attempted:fail("scope-conflict")
  if not isinstance(body,dict) or set(body)!={"body"} or not isinstance(body["body"],str):fail("scope-conflict")
  marker=self.r.PINNED[1][1]
  if not body["body"].startswith(marker):fail("scope-conflict")
  v=self.r.strict(body["body"][len(marker):])
  expected={"schema":2,"sequence":1,"highWater":v.get("highWater"),"previousStateSHA256":STATE_SHA,
            "lastRunID":self.c["runID"],"lastRunNumber":3,"lastRunHeadSHA":self.c["headSHA"],"status":"blocked"}
  if v!=expected or body["body"]!=marker+self.r.encoded(expected):fail("scope-conflict")
  self.r.stamp(v["highWater"]);self.expected=dict(body)
 def patch(self,body):
  if self.attempted or self.expected is None or body!=self.expected:fail("scope-conflict")
  if self.reader.calls>29 or self.reader.until-time.monotonic()<=30:fail("budget")
  value=self.token()
  if not isinstance(value,str) or not 1<=len(value)<=16384 or any(c.isspace() for c in value):fail("token-invalid")
  raw=self.r.encoded(body).encode()
  if len(raw)>4096:fail("budget")
  # Mark before handing to HTTP. Never resend on unknown response, including within this process.
  self.attempted=True;self.reader.calls+=1;url=API+"/issues/1"
  headers={"Accept":"application/vnd.github+json","Content-Type":"application/json","User-Agent":"BuildShogiAdopt",
           "Cache-Control":"no-store","X-GitHub-Api-Version":"2022-11-28","Authorization":"Bearer "+value}
  try:
   with self.reader.opener.open(Request(url,method="PATCH",data=raw,headers=headers),
                                timeout=min(10,self.reader.until-time.monotonic())) as response:
    raw=response.read(524289)
    if response.geturl()!=url or response.status!=200 or len(raw)>524288 or time.monotonic()>=self.reader.until:fail("write-outcome-unknown")
    return self.r.strict(raw)
  except Exception:fail("write-outcome-unknown")

def adopt(r,c,http,now,*,progress=None):
 plan=proposal(r,c,http,now,progress=progress)
 # Last direct reads immediately before the sole PATCH.
 for n in (1,2):r.issue(http.get(API+"/issues/"+str(n)),n)
 http.arm(plan["request"])
 if progress is not None:progress({"event":"journal-adoption","stage":"patch"})
 response=http.patch(plan["request"]);readback(r,response,plan["after"])
 if progress is not None:progress({"event":"journal-adoption","stage":"readback"})
 readback(r,http.get(API+"/issues/1"),plan["after"])
 r.issue(http.get(API+"/issues/2"),2)
 if progress is not None:progress({"event":"journal-adoption","stage":"complete"})
 return {"schema":1,"status":"blocked","checkpointWriteConfirmed":True,"stateIssueWrites":1,"alertIssueWrites":0,
         "witnessRunID":WITNESS,"failedBootstrapRunID":r.FAILED,"failedBootstrapRemainsFailure":True,
         "adoptionRunID":c["runID"],"adoptionRunNumber":3,"adoptionHeadSHA":c["headSHA"],
         "previousStateSHA256":STATE_SHA,"afterStateSHA256":plan["afterStateSHA256"],
         "monitoringEnabled":False,"collectionEnabled":False,"deletionEnabled":False,
         "productionCoverageVerified":False,"requiresThisRunFinalSuccess":True,"delivered":False}

def failure_result(exc):
 reason=str(exc) if type(exc) is ValueError and str(exc) in REASONS else "unclassified-error"
 return {"status":"blocked","outcome":"adoption-unconfirmed-no-retry","reason":reason,
         "monitoringEnabled":False,"collectionEnabled":False,"deletionEnabled":False}

def main():
 parser=argparse.ArgumentParser();parser.add_argument("--execute-approved-adoption",action="store_true");args=parser.parse_args()
 if not args.execute_approved_adoption:
  print('{"executed":false,"status":"inactive-adoption-draft"}');return
 if os.environ.get("BUILDSHOGI_ADOPTION_APPROVED")!="true":fail("approval-required")
 _,r=local_binding(Path(__file__).resolve().parents[2])
 c=context({"repository":os.environ.get("GITHUB_REPOSITORY"),"runID":int(os.environ.get("GITHUB_RUN_ID","0")),
            "headSHA":os.environ.get("GITHUB_SHA"),"attempt":int(os.environ.get("GITHUB_RUN_ATTEMPT","0")),
            "ref":os.environ.get("GITHUB_REF"),"concurrency":os.environ.get("BUILDSHOGI_CONCURRENCY")})
 result=adopt(r,c,HTTP(r,c,token=lambda:os.environ.get("GH_TOKEN")),datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              progress=lambda v:print(encoded(v),flush=True))
 print(encoded(result))
if __name__=="__main__":
 try:main()
 except Exception as exc:print(encoded(failure_result(exc)),flush=True);raise SystemExit(1)
