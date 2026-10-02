"""Which listing of the jobs wins, and when the list is asked for at all.

Refreshes of the Jobs list overlap: the tab opening, the polling ladder,
Refresh, a filter change, every action's own follow-up, and now a change in
tts-long's queue seen by health. The last answer to land used to win, so an
answer could be drawn under a filter that was no longer chosen, or put back
a row deleted while it was out. These run the page's own source from
`const JOBS_SEQ = {` to `function ping(job)` in Node, with the listing's
answers held and released in whatever order a scenario chooses. Without
`node` on PATH they skip, with the reason.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "app" / "static" / "ui.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH; these drive the "
                                "page's own JavaScript and need a JavaScript runtime")

# Every GET of /jobs waits in `out` until the scenario answers it:
# land(i, jobs) answers the i-th request made. renderJobs only counts.
HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const from = html.indexOf("const JOBS_SEQ = {");
const to = html.indexOf("\nfunction ping(job)", from);
if (from < 0 || to < 0) throw new Error("the jobs listing's bounds moved");
const SECTION = html.slice(from, to);
const SCENARIO = fs.readFileSync(process.argv[3], "utf8");

const out = [];
function json(path) {
  return new Promise(resolve => out.push({ path, resolve }));
}
async function land(i, listed) {
  out[i].resolve({ jobs: listed, counts: null, truncated: false });
  await new Promise(r => setImmediate(r));
}
const job = (id, status) => ({ id, status: status || "done", created_at: 1 });
let renders = 0;
const renderJobs = () => { renders++; };
// The jobs this browser started, which a scenario may fill.
let seen = [];
const remembered = () => seen;
const store = { set() {} };
const jobsKey = () => "jobs.u_aaaaaaaaaaaaaaaa";
let owner = "me";
const jobOwner = () => owner;
const jobs = new Map();
const TTL = 86400;
const OFFSETS_SEEN = new Map();
let growing = false, jobCounts = null, jobTruncated = false;
const JOB_FILTERS = { playable: { query: { audio: "present" } }, failed: { query: { status: "failed" } },
                      all: { query: {} } };
let filter = "playable", kind = "all";
const jobFilter = () => filter, jobKind = () => kind;
const ping = () => {};
const LOADED = { glossaries: 0, voices: 0, jobs: 0 };
const scheduled = [];
const schedule = delay => scheduled.push(delay);
const panels = { "tab-jobs": { hidden: false } };
const $ = id => panels[id];
const document = { hidden: false };
// What the session holds; a scenario may take jobs:read:own away.
const held = new Set(["jobs:read:own"]);
const holds = scope => held.has(scope);

eval(SECTION + "\n;(async () => {\n" + SCENARIO + "\n})().catch(e => { console.error(e); process.exit(2); });");
"""


def run(tmp_path: Path, scenario: str):
    """The scenario's last line prints one JSON value; that value."""
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "scenario.js").write_text(scenario)
    done = subprocess.run([NODE, str(tmp_path / "harness.js"), str(PAGE),
                           str(tmp_path / "scenario.js")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr or done.stdout
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_listing_that_lands_after_a_newer_one_is_dropped(tmp_path):
    got = run(tmp_path, """
      const older = refreshJobsInner(), newer = refreshJobsInner();
      await land(1, [job("new")]);
      await land(0, [job("old")]);
      await Promise.all([older, newer]);
      console.log(JSON.stringify({ jobs: [...jobs.keys()], renders }));
    """)
    assert got == {"jobs": ["new"], "renders": 1}, got


def test_a_listing_asked_for_under_another_filter_is_dropped(tmp_path):
    """Playable left, then the reader chose Failures: the Playable answer
    must not be drawn under the Failures label."""
    got = run(tmp_path, """
      const asked = refreshJobsInner();
      filter = "failed";
      await land(0, [job("playable")]);
      await asked;
      const dropped = { jobs: [...jobs.keys()], renders };
      kind = "clone";
      const again = refreshJobsInner();
      kind = "all";
      await land(1, [job("a clone")]);
      await again;
      console.log(JSON.stringify({ dropped, kind: [...jobs.keys()], path: out[0].path }));
    """)
    assert got["dropped"] == {"jobs": [], "renders": 0}, got
    assert got["kind"] == [], got
    assert got["path"] == "/jobs?audio=present", got


def test_an_admin_listing_asks_for_whose_runs_it_shows_and_drops_another_owners_answer(tmp_path):
    """Everyone's runs are asked for by name (?owner=all, D32), and an answer
    asked for under one owner is not drawn under another: Mine left, then the
    reader chose Everyone's, and Mine's answer must not fill that list."""
    got = run(tmp_path, """
      owner = "all";
      const first = refreshJobsInner();
      await land(0, [job("everyone's")]);
      await first;
      const listed = [...jobs.keys()];
      const second = refreshJobsInner();
      owner = "system";
      await land(1, [job("all of them")]);
      await second;
      console.log(JSON.stringify({ path: out[0].path, listed, dropped: [...jobs.keys()] }));
    """)
    assert got["path"] == "/jobs?audio=present&owner=all", got
    assert got["listed"] == ["everyone's"], got
    assert got["dropped"] == ["everyone's"], got


def test_an_admins_own_live_job_is_not_called_lost_by_a_listing_of_someone_elses(tmp_path):
    """tts-long applies `owner` before its rule that a live job comes back
    from every filter, so the system's runs never hold the admin's own clone.
    Taken as lost, it became "lost when the service restarted" with a live
    Retry, and Retry queued the same GPU job again."""
    got = run(tmp_path, """
      seen = [{ id: "mine", at: Date.now() / 1000 }];
      owner = "system";
      jobs.set("mine", job("mine", "running"));
      const asked = refreshJobsInner();
      await land(0, [job("the system's")]);
      await asked;
      const theirs = [...jobs.values()];
      // Under the reader's own runs the same absence still means lost.
      owner = "me";
      jobs.set("mine", job("mine", "running"));
      const again = refreshJobsInner();
      await land(1, [job("the system's")]);
      await again;
      console.log(JSON.stringify({ path: out[0].path, theirs, lost: jobs.get("mine") }));
    """)
    assert got["path"] == "/jobs?audio=present&owner=system", got
    assert got["theirs"] == [{"id": "the system's", "status": "done", "created_at": 1}], got
    assert got["lost"]["status"] == "failed", got
    assert got["lost"]["error"] == "lost when the service restarted", got


def test_a_job_deleted_while_a_poll_is_in_flight_does_not_come_back(tmp_path):
    """The poll left holding the job; the reader deleted it; the poll landed.
    Merged, the row came back for one interval, looking like a delete that
    had not worked."""
    got = run(tmp_path, """
      jobs.set("gone", job("gone"));
      const asked = refreshJobsInner();
      JOBS_SEQ.writes++;                 // forgetJob's DELETE has answered
      jobs.delete("gone");
      await land(0, [job("gone"), job("kept")]);
      await asked;
      console.log(JSON.stringify({ jobs: [...jobs.keys()], renders }));
    """)
    assert got == {"jobs": [], "renders": 0}, got


def test_a_stale_mark_asks_even_with_the_jobs_tab_closed(tmp_path):
    """Health saw tts-long's queue move: somebody queued a job from another
    device. The list is asked for at once whatever tab is open, until an
    answer has been drawn."""
    got = run(tmp_path, """
      panels["tab-jobs"].hidden = true;
      const before = jobsDue();
      jobsStale();
      const stale = jobsDue();
      const asked = refreshJobsInner();
      await land(0, [job("elsewhere", "queued")]);
      await asked;
      const live = jobsDue();
      jobs.clear();
      console.log(JSON.stringify({ before, stale, scheduled, live, after: jobsDue() }));
    """)
    assert got == {"before": False, "stale": True, "scheduled": [0], "live": True, "after": False}, got


def test_a_session_that_may_not_read_jobs_is_never_due_to_ask(tmp_path):
    """The user role holds no jobs scope: not the tab, not a live job this
    browser remembers, not a queue that moved, is a reason to ask /jobs."""
    got = run(tmp_path, """
      held.delete("jobs:read:own");
      const shown = jobsDue();
      jobs.set("running", job("running", "running"));
      const live = jobsDue();
      jobsStale();
      console.log(JSON.stringify({ shown, live, stale: jobsDue(), asked: out.length }));
    """)
    assert got == {"shown": False, "live": False, "stale": False, "asked": 0}, got


def test_a_hidden_page_with_nothing_live_asks_for_no_jobs(tmp_path):
    got = run(tmp_path, """
      const shown = jobsDue();
      document.hidden = true;
      const hidden = jobsDue();
      jobs.set("running", job("running", "running"));
      const live = jobsDue();
      console.log(JSON.stringify({ shown, hidden, live }));
    """)
    assert got == {"shown": True, "hidden": False, "live": True}, got
