"""Local proposal: one bot-owned checkpoint bootstrap. Default is inactive.

No Firestore/health/ACK/deletion/schedule. No token read until the exact closed
POST plan is armed after baseline/history and duplicate checks. Unknown POST
stops immediately. Same-workflow run-number/attempt guards survive disk loss.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode,urlsplit,parse_qs
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
import buildshogi_monitor as m

ISSUES=m.API+"/issues"
PREFIXES=("key: buildshogi-ops-observer-state/","key: buildshogi-ops-health-alert/")
BASELINE_ID=41179058909
BASELINE_HEAD="c99b8fa38f6c7303913a272c4b36e0c1e7af2c05"
WORKFLOW_ID=402524099

def config(v):
    keys={"schema","mode","enabled","workflowID","baselineRunID","baselineRunNumber","baselineHeadSHA",
          "expectedRunNumber","monitorSHA256","bootstrapSHA256","workflowSHA256"}
    if (not isinstance(v,dict) or set(v)!=keys or type(v["schema"]) is not int or v["schema"]!=2
        or v["mode"]!="bootstrap-only" or v["enabled"] is not True
        or type(v["workflowID"]) is not int or v["workflowID"]!=WORKFLOW_ID
        or type(v["baselineRunID"]) is not int or v["baselineRunID"]!=BASELINE_ID
        or type(v["baselineRunNumber"]) is not int or v["baselineRunNumber"]!=1
        or v["baselineHeadSHA"]!=BASELINE_HEAD or type(v["expectedRunNumber"]) is not int or v["expectedRunNumber"]!=2
        or any(not isinstance(v[k],str) or not m.SHA.fullmatch(v[k]) for k in ["monitorSHA256","bootstrapSHA256","workflowSHA256"])):
        m.fail("exact-reviewed-bootstrap-binding-required")
    return v

def local_binding(root):
    root=Path(root)
    def reviewed_file(relative):
        path=root/relative
        if (any(p.is_symlink() for p in [root,*[root.joinpath(*Path(relative).parts[:i]) for i in range(1,len(Path(relative).parts)+1)]])
            or not path.is_file() or path.stat().st_size>65536):
            m.fail("bounded-regular-bootstrap-source-required")
        return path
    v=config(m.strict(reviewed_file(".github/buildshogi-monitor-binding.json").read_text()))
    for relative,key in [(".github/scripts/buildshogi_monitor.py","monitorSHA256"),
                         (".github/scripts/buildshogi_bootstrap.py","bootstrapSHA256"),
                         (m.WORKFLOW,"workflowSHA256")]:
        path=reviewed_file(relative)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=v[key]:
            m.fail("reviewed-bootstrap-source-hash-required")
    return v

def context(v):
    if (not isinstance(v,dict) or set(v)!={"repository","runID","headSHA","runAttempt","ref","concurrency"}
        or v["repository"]!=m.REPO or not m.positive(v["runID"]) or type(v["runAttempt"]) is not int
        or v["runAttempt"]!=1 or v["ref"]!="refs/heads/main" or v["concurrency"]!=m.GROUP
        or not isinstance(v["headSHA"],str) or not re.fullmatch(r"[a-f0-9]{40}",v["headSHA"])):
        m.fail("fixed-bootstrap-context-required")
    return v

def page_url(page):
    if type(page) is not int or not 1<=page<=10:m.fail("bootstrap-issue-page-budget")
    return ISSUES+"?"+urlencode({"state":"all","sort":"created","direction":"asc","per_page":100,"page":page})

def next_page(link,page):
    if not isinstance(link,str) or len(link)>4096:m.fail("invalid-bootstrap-pagination")
    result=None
    for item in link.split(",") if link else []:
        match=re.fullmatch(r'\s*<([^>]+)>;\s*rel="(next|last|first|prev)"\s*',item)
        if not match:m.fail("invalid-bootstrap-pagination")
        u=urlsplit(match[1])
        q=parse_qs(u.query,keep_blank_values=True)
        try:n=int(q["page"][0])
        except (KeyError,ValueError,IndexError):m.fail("invalid-bootstrap-pagination")
        if (u.scheme!="https" or u.netloc!="api.github.com" or u.path!="/repos/"+m.REPO+"/issues"
            or u.fragment or q!=parse_qs(urlsplit(page_url(n)).query)):
            m.fail("foreign-bootstrap-pagination")
        if match[2]=="next":
            if result is not None or n!=page+1:m.fail("ambiguous-bootstrap-pagination")
            result=n
    return result

def scan_issues(http,allowed=None):
    page=1;seen=set()
    for _ in range(10):
        rows=http.send("GET",page_url(page))
        if not isinstance(rows,list) or len(rows)>100:m.fail("closed-bootstrap-issue-page-required")
        for row in rows:
            if not isinstance(row,dict):m.fail("invalid-bootstrap-issue-row")
            title,body=row.get("title"),row.get("body")
            if title in {m.STATE_TITLE,m.ALERT_TITLE} or isinstance(body,str) and body.startswith(PREFIXES):
                number=row.get("number")
                if allowed is None or number not in allowed or number in seen:
                    m.fail("existing-or-conflicting-issue-manual-reconciliation-required")
                title,marker,expected=allowed[number]
                if m.issue(row,number,title,marker)!=expected:m.fail("bootstrap-final-issue-conflict")
                seen.add(number)
        n=next_page(http.last_link,page)
        if n is None:
            if allowed is not None and seen!=set(allowed):m.fail("bootstrap-final-issue-missing")
            return
        page=n
    m.fail("bootstrap-pagination-not-complete")

class HTTP:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    def __init__(self,b,current_id,*,token):
        config(b)
        if not m.positive(current_id):m.fail("bootstrap-run-required")
        self.b=b;self.token=token;self.calls=0;self.until=time.monotonic()+145
        self.reads={m.API+"/actions/runs/"+str(i) for i in [current_id,b["baselineRunID"]]}
        self.last_link="";self.armed=None;self.post_index=0
        self.opener=build_opener(ProxyHandler({}),self.NoRedirect())
    def arm(self,requests):
        if self.armed is not None or len(requests)!=2:m.fail("bootstrap-plan-already-armed")
        for request,title,marker in zip(requests,[m.STATE_TITLE,m.ALERT_TITLE],[m.MARKER,m.ALERT_MARKER]):
            if set(request)!={"method","url","body"} or request["method"]!="POST" or request["url"]!=ISSUES:
                m.fail("fixed-bootstrap-post-plan-required")
            body=request["body"]
            if set(body)!={"title","body","state"} or body["title"]!=title or body["state"]!="open" or not body["body"].startswith(marker):
                m.fail("fixed-bootstrap-body-required")
            parsed=m.strict(body["body"][len(marker):])
            if marker+m.encoded(parsed)!=body["body"]:m.fail("canonical-bootstrap-body-required")
            if title==m.STATE_TITLE:m.state(parsed)
            elif parsed!={"schema":2,"status":"blocked","reason":"coverage-unverified"}:m.fail("fixed-bootstrap-alert-required")
        self.armed=[m.encoded(r["body"]) for r in requests]
    def allow_issue(self,number):
        if not m.positive(number):m.fail("created-issue-number-required")
        self.reads.add(ISSUES+"/"+str(number))
    def send(self,method,url,body=None):
        list_read=url in {page_url(i) for i in range(1,11)}
        if method=="GET":
            if body is not None or url not in self.reads and not list_read:m.fail("outside-fixed-bootstrap-read")
        elif method=="POST":
            if (url!=ISSUES or self.armed is None or self.post_index>=2 or m.encoded(body)!=self.armed[self.post_index]):
                m.fail("outside-exact-bootstrap-post")
        else:m.fail("bootstrap-method-forbidden")
        if self.calls>=40 or time.monotonic()>=self.until:m.fail("bootstrap-budget")
        data=None if body is None else m.encoded(body).encode()
        if data is not None and len(data)>16384:m.fail("bootstrap-request-budget")
        headers={"Accept":"application/vnd.github+json","User-Agent":"BuildShogiBootstrap","Cache-Control":"no-store","X-GitHub-Api-Version":"2022-11-28"}
        if method=="POST":
            self.post_index+=1 # Never retry this body, even after provider failure.
            value=self.token()
            if not isinstance(value,str) or not 1<=len(value)<=16384 or any(c.isspace() for c in value):m.fail("invalid-bootstrap-runtime-token")
            headers.update(Authorization="Bearer "+value,**{"Content-Type":"application/json"})
        self.calls+=1
        try:
            with self.opener.open(Request(url,method=method,data=data,headers=headers),timeout=min(10,self.until-time.monotonic())) as r:
                raw=r.read(524289)
                if r.geturl()!=url or r.status!=(201 if method=="POST" else 200) or len(raw)>524288 or time.monotonic()>=self.until:m.fail("bootstrap-response-budget")
                self.last_link=r.headers.get("Link","") if list_read else ""
                if len(self.last_link)>4096:m.fail("bootstrap-link-budget")
                value=m.strict(raw)
                for row in value if isinstance(value,list) else [value]:
                    if isinstance(row,dict) and isinstance(row.get("user"),dict):
                        row["user"]={k:row["user"].get(k) for k in ["login","type"]}
                return value
        except Exception:m.fail("bootstrap-write-outcome-unknown" if method=="POST" else "bootstrap-read-unconfirmed")

def bootstrap(b,c,http,now):
    config(b);context(c);m.stamp(now)
    current=m.run(http.send("GET",m.API+"/actions/runs/"+str(c["runID"])),b,run_id=c["runID"],number=b["expectedRunNumber"],head=c["headSHA"],current=True)
    prior=m.run(http.send("GET",m.API+"/actions/runs/"+str(b["baselineRunID"])),b,run_id=b["baselineRunID"],number=b["baselineRunNumber"],head=b["baselineHeadSHA"])
    proposal=m.bootstrap_proposal(b["workflowID"],prior,now,current=current)
    scan_issues(http)
    scan_issues(http) # Immediate second complete snapshot before arming any write.
    http.arm(proposal["requests"])
    numbers=[];expected_issues={}
    for request,marker in zip(proposal["requests"],[m.MARKER,m.ALERT_MARKER]):
        expected=m.strict(request["body"]["body"][len(marker):])
        created=http.send("POST",request["url"],request["body"])
        number=created.get("number") if isinstance(created,dict) else None
        if not m.positive(number) or number in numbers:m.fail("bootstrap-created-issue-unconfirmed")
        if m.issue(created,number,request["body"]["title"],marker)!=expected:m.fail("bootstrap-create-response-conflict")
        http.allow_issue(number)
        if m.issue(http.send("GET",ISSUES+"/"+str(number)),number,request["body"]["title"],marker)!=expected:
            m.fail("bootstrap-readback-conflict")
        numbers.append(number)
        expected_issues[number]=(request["body"]["title"],marker,expected)
    scan_issues(http,expected_issues)
    return {"status":"blocked","bootstrapConfirmed":True,"stateIssue":numbers[0],"alertIssue":numbers[1],
            "anchorRunID":current["id"],"anchorRunNumber":current["run_number"],"anchorRunHeadSHA":current["head_sha"],
            "initialStateSHA256":m.digest(m.strict(proposal["requests"][0]["body"]["body"][len(m.MARKER):])),
            "delivered":False,"monitoringEnabled":False}

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--execute-approved-bootstrap",action="store_true")
    args=parser.parse_args()
    if not args.execute_approved_bootstrap:
        print('{"executed":false,"status":"inactive-bootstrap-draft"}');return
    if os.environ.get("BUILDSHOGI_BOOTSTRAP_APPROVED")!="true":m.fail("explicit-bootstrap-deployment-approval-required")
    root=Path(__file__).resolve().parents[2]
    b=local_binding(root)
    c=context({"repository":os.environ.get("GITHUB_REPOSITORY"),"runID":int(os.environ.get("GITHUB_RUN_ID","0")),
               "headSHA":os.environ.get("GITHUB_SHA"),"runAttempt":int(os.environ.get("GITHUB_RUN_ATTEMPT","0")),
               "ref":os.environ.get("GITHUB_REF"),"concurrency":os.environ.get("BUILDSHOGI_CONCURRENCY")})
    http=HTTP(b,c["runID"],token=lambda:os.environ.get("GH_TOKEN"))
    print(m.encoded(bootstrap(b,c,http,datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))))
if __name__=="__main__":
    try:main()
    except Exception:
        print('{"status":"blocked","outcome":"bootstrap-unconfirmed-manual-reconciliation-required"}')
        raise SystemExit(1)
