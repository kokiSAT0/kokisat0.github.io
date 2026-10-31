"""Inactive deployment draft. Fixed non-PII Issues; no Firestore or secrets discovery.

Workflow run IDs/numbers are the durable journal. Missing/failed/rerun/deleted
history is a hold, never auto-bootstrap. No automatic write retries.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

REPO="kokiSAT0/kokisat0.github.io"
API="https://api.github.com/repos/"+REPO
HEALTH="https://kokisat0.github.io/buildshogi/ops-health.json"
WORKFLOW=".github/workflows/buildshogi-ops-monitor.yml"
GROUP="buildshogi-operations-monitor"
STATE_TITLE="[BuildShogi] operations observer state"
ALERT_TITLE="[BuildShogi] operations health alert"
MARKER="key: buildshogi-ops-observer-state/v2\n"
ALERT_MARKER="key: buildshogi-ops-health-alert/v2\n"
SHA=re.compile(r"[a-f0-9]{64}")

def encoded(v):return json.dumps(v,sort_keys=True,separators=(",",":"),allow_nan=False)
def digest(v):return hashlib.sha256(encoded(v).encode()).hexdigest()
def strict(raw):
    def pairs(items):
        value={}
        for k,v in items:
            if k in value:raise ValueError("duplicate-json-key")
            value[k]=v
        return value
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:fail("nonfinite-json"))
def fail(reason):raise ValueError(reason)
def positive(v):return type(v) is int and 0<v<2**53
def stamp(v):
    if not isinstance(v,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",v):fail("utc-seconds-required")
    return datetime.strptime(v,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
def health_time(v):
    if not isinstance(v,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z",v):fail("invalid-health-time")
    seconds=v[:19]+"Z"
    base=stamp(seconds)
    if base.strftime("%Y-%m-%dT%H:%M:%SZ")!=seconds:fail("invalid-health-time")
    fraction=v[20:-1] if len(v)>20 else ""
    return int(base.timestamp())*10**9+int((fraction+"0"*9)[:9])
def binding(b):
    keys={"schema","enabled","workflowID","stateIssue","alertIssue","scriptSHA256","workflowSHA256","productionCoverageVerified"}
    if (not isinstance(b,dict) or set(b)!=keys or type(b["schema"]) is not int or b["schema"]!=1
        or b["enabled"] is not True or any(not positive(b[k]) for k in ["workflowID","stateIssue","alertIssue"])
        or b["stateIssue"]==b["alertIssue"] or b["productionCoverageVerified"] is not False
        or any(not isinstance(b[k],str) or not SHA.fullmatch(b[k]) for k in ["scriptSHA256","workflowSHA256"])):
        fail("reviewed-binding-required-no-auto-bootstrap")
    return b
def state(v):
    keys={"schema","sequence","highWater","previousStateSHA256","lastRunID","lastRunNumber","lastRunHeadSHA","status"}
    if (not isinstance(v,dict) or set(v)!=keys or type(v["schema"]) is not int or v["schema"]!=2
        or type(v["sequence"]) is not int or not 0<=v["sequence"]<2**53
        or not positive(v["lastRunID"]) or not positive(v["lastRunNumber"])
        or not re.fullmatch(r"[0-9a-f]{40}",v["lastRunHeadSHA"]) or v["status"]!="blocked"
        or (v["sequence"]==0 and v["previousStateSHA256"] is not None)
        or (v["sequence"]>0 and (not isinstance(v["previousStateSHA256"],str) or not SHA.fullmatch(v["previousStateSHA256"])))):
        fail("invalid-external-journal-state")
    stamp(v["highWater"]);return v
def issue(v,number,title,marker):
    if (not isinstance(v,dict) or v.get("number")!=number or v.get("title")!=title or v.get("state")!="open"
        or "pull_request" in v or v.get("user")!={"login":"github-actions[bot]","type":"Bot"}
        or not isinstance(v.get("body"),str) or not v["body"].startswith(marker) or len(v["body"])>4096):
        fail("pinned-bot-issue-conflict")
    parsed=strict(v["body"][len(marker):])
    if marker+encoded(parsed)!=v["body"]:fail("noncanonical-issue-body")
    return parsed
def run(v,b,*,run_id=None,number=None,head=None,current=False):
    if (not isinstance(v,dict) or not positive(v.get("id")) or not positive(v.get("run_number"))
        or v.get("workflow_id")!=b["workflowID"] or v.get("run_attempt")!=1
        or v.get("repository",{}).get("full_name")!=REPO or v.get("head_branch")!="main"
        or v.get("path")!=WORKFLOW or not re.fullmatch(r"[a-f0-9]{40}",v.get("head_sha",""))
        or (run_id is not None and v["id"]!=run_id) or (number is not None and v["run_number"]!=number)
        or (head is not None and v["head_sha"]!=head)
        or (current and (v.get("status")!="in_progress" or v.get("conclusion") is not None))
        or (not current and (v.get("status")!="completed" or v.get("conclusion")!="success"))):
        fail("workflow-journal-history-unconfirmed")
    return v

def bootstrap_proposal(workflow_id,baseline,now):
    """Review-only bot Issue requests. Never invoked by the workflow."""
    if not positive(workflow_id):fail("pinned-workflow-required")
    run(baseline,{"workflowID":workflow_id})
    stamp(now)
    initial=state({"schema":2,"sequence":0,"highWater":now,"previousStateSHA256":None,
                   "lastRunID":baseline["id"],"lastRunNumber":baseline["run_number"],
                   "lastRunHeadSHA":baseline["head_sha"],"status":"blocked"})
    alert={"schema":2,"status":"blocked","reason":"coverage-unverified"}
    requests=[{"method":"POST","url":API+"/issues","body":{"title":title,"body":marker+encoded(value),"state":"open"}}
              for title,marker,value in [(STATE_TITLE,MARKER,initial),(ALERT_TITLE,ALERT_MARKER,alert)]]
    return {"executed":False,"requests":requests,"requestsSHA256":digest(requests),
            "requiresSeparateBootstrapApproval":True,"requiredBot":"github-actions[bot]"}

class HTTP:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    def __init__(self,b,current_id,*,token):
        self.b=b;self.token=token;self.calls=0;self.until=time.monotonic()+145
        self.reads={HEALTH,API+"/actions/runs/"+str(current_id),API+"/issues/"+str(b["stateIssue"]),API+"/issues/"+str(b["alertIssue"])}
        self.opener=build_opener(ProxyHandler({}),self.NoRedirect())
    def allow_prior(self,run_id):
        if not positive(run_id):fail("invalid-prior-run")
        self.reads.add(API+"/actions/runs/"+str(run_id))
    def send(self,method,url,body=None):
        writes={API+"/issues/"+str(self.b[k]) for k in ["stateIssue","alertIssue"]}
        if not ((method=="GET" and url in self.reads and body is None) or (method=="PATCH" and url in writes and isinstance(body,dict))):
            fail("outside-fixed-monitor-scope")
        if self.calls>=16 or time.monotonic()>=self.until:fail("monitor-budget")
        data=None if body is None else encoded(body).encode()
        if data is not None and len(data)>16384:fail("request-budget")
        headers={"Accept":"application/vnd.github+json","User-Agent":"BuildShogiOps","Cache-Control":"no-store","X-GitHub-Api-Version":"2022-11-28"}
        # Public GET never sends a token, especially not to Pages.
        if method=="PATCH":
            value=self.token()
            if not isinstance(value,str) or not 1<=len(value)<=16384 or any(c.isspace() for c in value):fail("invalid-runtime-token")
            headers["Authorization"]="Bearer "+value;headers["Content-Type"]="application/json"
        self.calls+=1
        try:
            with self.opener.open(Request(url,method=method,data=data,headers=headers),timeout=min(10,self.until-time.monotonic())) as r:
                raw=r.read(524289)
                if r.geturl()!=url or not 200<=r.status<300 or len(raw)>524288 or time.monotonic()>=self.until:fail("response-budget")
                value=strict(raw)
                # API carries many harmless user fields; project only identity.
                if url.startswith(API+"/issues/") and isinstance(value,dict) and isinstance(value.get("user"),dict):
                    value["user"]={k:value["user"].get(k) for k in ["login","type"]}
                return value
        except Exception:fail("write-outcome-unknown" if method=="PATCH" else "read-unconfirmed")

def cycle(b,context,http,now):
    binding(b);stamp(now)
    if set(context)!={"repository","runID","headSHA","runAttempt","ref","concurrency"} or context["repository"]!=REPO or context["runAttempt"]!=1 or context["ref"]!="refs/heads/main" or context["concurrency"]!=GROUP or not positive(context["runID"]):
        fail("fixed-workflow-context-required")
    current=run(http.send("GET",API+"/actions/runs/"+str(context["runID"])),b,run_id=context["runID"],head=context["headSHA"],current=True)
    state_url=API+"/issues/"+str(b["stateIssue"]);alert_url=API+"/issues/"+str(b["alertIssue"])
    before=state(issue(http.send("GET",state_url),b["stateIssue"],STATE_TITLE,MARKER))
    if stamp(now)<stamp(before["highWater"]):fail("clock-rollback-hold")
    if current["run_number"]!=before["lastRunNumber"]+1:fail("journal-gap-no-automatic-recovery")
    http.allow_prior(before["lastRunID"])
    run(http.send("GET",API+"/actions/runs/"+str(before["lastRunID"])),b,run_id=before["lastRunID"],number=before["lastRunNumber"],head=before["lastRunHeadSHA"])
    alert_before=http.send("GET",alert_url)
    prior_alert=issue(alert_before,b["alertIssue"],ALERT_TITLE,ALERT_MARKER)
    if not isinstance(prior_alert,dict) or set(prior_alert)!={"schema","status","reason"} or prior_alert["schema"]!=2 or prior_alert["status"]!="blocked" or prior_alert["reason"] not in {"coverage-unverified","health-unavailable","health-blocked","health-stale"}:fail("invalid-alert-state")
    reason="coverage-unverified" # Never promote read/synthetic success to production sweep.
    try:
        h=http.send("GET",HEALTH)
        if not isinstance(h,dict) or set(h)!={"schema","status","lastSuccessfulSweepAt"} or type(h["schema"]) is not int or h["schema"]!=1 or h["status"] not in {"blocked","healthy"}:fail("invalid-health")
        if h["status"]=="healthy":
            age=health_time(now)-health_time(h["lastSuccessfulSweepAt"])
            if age<0:fail("future-health")
            if age>16*3600*10**9:reason="health-stale"
        else:reason="health-blocked"
    except Exception:reason="health-unavailable"
    alert={"schema":2,"status":"blocked","reason":reason}
    after={"schema":2,"sequence":before["sequence"]+1,"highWater":now,"previousStateSHA256":digest(before),
           "lastRunID":current["id"],"lastRunNumber":current["run_number"],"lastRunHeadSHA":current["head_sha"],"status":"blocked"}
    state(after)
    # Re-read both pinned targets immediately before writes. No list/create.
    if state(issue(http.send("GET",state_url),b["stateIssue"],STATE_TITLE,MARKER))!=before or http.send("GET",alert_url)!=alert_before:fail("changed-before-write")
    if prior_alert!=alert:
        request={"title":ALERT_TITLE,"body":ALERT_MARKER+encoded(alert),"state":"open"}
        http.send("PATCH",alert_url,request)
        if issue(http.send("GET",alert_url),b["alertIssue"],ALERT_TITLE,ALERT_MARKER)!=alert:fail("alert-write-unconfirmed")
    http.send("PATCH",state_url,{"title":STATE_TITLE,"body":MARKER+encoded(after),"state":"open"})
    if state(issue(http.send("GET",state_url),b["stateIssue"],STATE_TITLE,MARKER))!=after:fail("checkpoint-write-unconfirmed")
    return {"status":"blocked","writeConfirmed":True,"delivered":False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--execute-approved-monitor",action="store_true")
    args=parser.parse_args()
    if not args.execute_approved_monitor:
        print('{"executed":false,"status":"inactive-local-draft"}');return
    # No token read or network until all local gates and source pins pass.
    if os.environ.get("BUILDSHOGI_MONITOR_APPROVED")!="true":fail("explicit-deployment-gate-required")
    root=Path(__file__).resolve().parents[2]
    b=binding(strict((root/".github/buildshogi-monitor-binding.json").read_text()))
    for path,key in [(Path(__file__), "scriptSHA256"),(root/WORKFLOW,"workflowSHA256")]:
        if hashlib.sha256(path.read_bytes()).hexdigest()!=b[key]:fail("reviewed-source-hash-required")
    context={"repository":os.environ.get("GITHUB_REPOSITORY"),"runID":int(os.environ.get("GITHUB_RUN_ID","0")),
             "headSHA":os.environ.get("GITHUB_SHA"),"runAttempt":int(os.environ.get("GITHUB_RUN_ATTEMPT","0")),
             "ref":os.environ.get("GITHUB_REF"),"concurrency":os.environ.get("BUILDSHOGI_CONCURRENCY")}
    http=HTTP(b,context["runID"],token=lambda:os.environ.get("GH_TOKEN"))
    result=cycle(b,context,http,datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(encoded(result))
if __name__=="__main__":
    try:main()
    except Exception:
        print('{"status":"blocked","executedOutcome":"unconfirmed-manual-reconciliation-required"}')
        raise SystemExit(1)
