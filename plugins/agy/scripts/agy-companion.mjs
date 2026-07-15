#!/usr/bin/env node
// agy-companion: thin runtime wrapping the `agy` CLI, mirroring codex-companion.
// Subcommands: task, review, adversarial-review, status, result.
// agy has no native background/job store, so we add a file-based one here.
//
// Permission model (deliberate, safe-by-default):
//   default        -> read-only  (--mode plan)
//   --write        -> edits allowed but each tool still asks (--mode accept-edits)
//   --yolo         -> ONLY here do we add --dangerously-skip-permissions.
// The permission bypass is reachable exclusively via an explicit --yolo flag.
import { spawn, execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const AGY_HOME = path.join(os.homedir(), ".claude", "agy");
const JOBS_DIR = path.join(AGY_HOME, "jobs");
const CONFIG_PATH = path.join(AGY_HOME, "config.json");
const AGY_BIN = process.env.AGY_BIN || "agy";
const scriptPath = new URL(import.meta.url).pathname;

function readConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8")); } catch { return {}; }
}
function writeConfig(cfg) {
  fs.mkdirSync(AGY_HOME, { recursive: true, mode: 0o700 });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2), { mode: 0o600 });
}
const DEFAULT_MODEL = "Gemini 3.5 Flash (High)";
// Precedence: explicit --model flag > AGY_MODEL env > config.json > built-in default.
function resolveModel(flagModel) {
  return flagModel || process.env.AGY_MODEL || readConfig().model || DEFAULT_MODEL;
}
// A --model passed on the command line is persisted as the new default.
function persistModel(name) {
  const cfg = readConfig(); cfg.model = name; writeConfig(cfg);
}

function usage() {
  console.log(`Usage:
  agy-companion task [--background] [--write] [--yolo] [--resume] [--model <m>] [--print-timeout <d>] [prompt]
  agy-companion review [--base <ref>] [--scope <auto|working-tree|branch>]
  agy-companion adversarial-review [--base <ref>] [--scope <auto|working-tree|branch>] [focus text]
  agy-companion status [job-id] [--all]
  agy-companion result [job-id]
  agy-companion config [set-model "<name>" | clear-model]
  agy-companion hint

  Permissions: default read-only. --write allows edits (still prompts). --yolo bypasses prompts.`);
}

function newId(prefix) { return `${prefix}-${randomBytes(5).toString("hex")}`; }
function jobPath(id) { return path.join(JOBS_DIR, `${id}.json`); }
function writeJob(job) {
  // Job files hold prompts + agy output in plaintext. On shared multi-user
  // machines keep them owner-only: dir 0700, files 0600.
  fs.mkdirSync(JOBS_DIR, { recursive: true, mode: 0o700 });
  fs.writeFileSync(jobPath(job.id), JSON.stringify(job, null, 2), { mode: 0o600 });
  try { fs.chmodSync(jobPath(job.id), 0o600); } catch {} // enforce on overwrite of pre-existing file
}
function readJob(id) {
  try { return JSON.parse(fs.readFileSync(jobPath(id), "utf8")); } catch { return null; }
}
function latestJob() {
  let files;
  try { files = fs.readdirSync(JOBS_DIR).filter(f => f.endsWith(".json")); } catch { return null; }
  let best = null;
  for (const f of files) {
    const j = readJob(f.replace(/\.json$/, ""));
    if (j && (!best || j.startedAt > best.startedAt)) best = j;
  }
  return best;
}

// Build the agy argv for a prompt run. mode: 'plan' (read-only) | 'accept-edits'.
function buildAgyArgs({ prompt, mode, yolo, resume, model, printTimeout }) {
  const args = [];
  if (model) args.push("--model", model);
  if (resume) args.push("--continue");
  args.push("--mode", mode || "plan");
  if (yolo) args.push("--dangerously-skip-permissions"); // only via explicit --yolo
  if (printTimeout) args.push("--print-timeout", printTimeout);
  args.push("-p", prompt);
  return args;
}

function runAgyForeground(agyArgs, cwd) {
  return new Promise((resolve) => {
    const child = spawn(AGY_BIN, agyArgs, { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let out = "", err = "";
    child.stdout.on("data", d => out += d);
    child.stderr.on("data", d => err += d);
    child.on("close", code => resolve({ code, out, err }));
    child.on("error", e => resolve({ code: -1, out, err: String(e) }));
  });
}

function gitDiff(cwd, base, scope) {
  const run = (a) => execFileSync("git", a, { cwd, encoding: "utf8", maxBuffer: 32 * 1024 * 1024 });
  let eff = scope;
  if (!eff || eff === "auto") {
    const wt = run(["diff"]).trim() || run(["diff", "--cached"]).trim();
    eff = wt ? "working-tree" : "branch";
  }
  if (eff === "branch") return run(["diff", `${base || "origin/HEAD"}...HEAD`]);
  return run(["diff", "HEAD"]);
}

async function cmdTask(argv) {
  const o = parseFlags(argv, { bools: ["background", "write", "yolo", "resume"], vals: ["model", "print-timeout"] });
  const prompt = o._.join(" ").trim();
  if (!prompt) { console.error("task: prompt required"); process.exit(2); }
  const cwd = process.cwd();
  if (o.model) persistModel(o.model); // --model doubles as "remember this default"
  const mode = (o.write || o.yolo) ? "accept-edits" : "plan";
  const agyArgs = buildAgyArgs({
    prompt, mode, yolo: o.yolo, resume: o.resume, model: resolveModel(o.model),
    printTimeout: o["print-timeout"] || (o.background ? "30m" : undefined),
  });

  if (!o.background) {
    const r = await runAgyForeground(agyArgs, cwd);
    process.stdout.write(r.out);
    if (r.code !== 0 && r.err) process.stderr.write(r.err);
    process.exit(r.code === 0 ? 0 : 1);
  }
  const id = newId("agytask");
  writeJob({ id, kind: "task", status: "running", cwd, prompt, startedAt: Date.now(), agyArgs });
  const worker = spawn(process.execPath, [scriptPath, "_worker", id], { cwd, detached: true, stdio: "ignore" });
  worker.unref();
  console.log(JSON.stringify({ jobId: id, status: "running" }, null, 2));
}

async function cmdWorker(argv) {
  const job = readJob(argv[0]);
  if (!job) process.exit(1);
  const r = await runAgyForeground(job.agyArgs, job.cwd);
  job.status = r.code === 0 ? "done" : "failed";
  job.finishedAt = Date.now();
  job.output = r.out;
  if (r.err) job.stderr = r.err;
  writeJob(job);
}

async function cmdReview(argv, adversarial) {
  const o = parseFlags(argv, { bools: [], vals: ["base", "scope"] });
  const cwd = process.cwd();
  let diff;
  try { diff = gitDiff(cwd, o.base, o.scope); }
  catch (e) { console.error("review: git diff failed: " + e.message); process.exit(2); }
  if (!diff.trim()) { console.log("No changes to review."); return; }
  const focus = o._.join(" ").trim();
  const header = adversarial
    ? `You are an adversarial code reviewer. Hunt for bugs, security holes, data-loss and edge-case failures in the diff below. Assume it is broken; prove it. Be specific: file, line, failure scenario.${focus ? " Focus: " + focus : ""}`
    : `Review the diff below for correctness bugs and clear simplifications. One finding per line: location, problem, fix. No praise.`;
  const prompt = `${header}\n\n\`\`\`diff\n${diff}\n\`\`\``;
  const r = await runAgyForeground(buildAgyArgs({ prompt, mode: "plan", model: resolveModel(), printTimeout: "15m" }), cwd);
  process.stdout.write(r.out);
  if (r.code !== 0 && r.err) process.stderr.write(r.err);
  process.exit(r.code === 0 ? 0 : 1);
}

function cmdStatus(argv) {
  const o = parseFlags(argv, { bools: ["all"], vals: [] });
  if (o.all) {
    let files;
    try { files = fs.readdirSync(JOBS_DIR).filter(f => f.endsWith(".json")); } catch { files = []; }
    const jobs = files.map(f => readJob(f.replace(/\.json$/, ""))).filter(Boolean)
      .sort((a, b) => b.startedAt - a.startedAt)
      .map(j => ({ jobId: j.id, kind: j.kind, status: j.status }));
    console.log(JSON.stringify(jobs, null, 2));
    return;
  }
  const job = o._[0] ? readJob(o._[0]) : latestJob();
  if (!job) { console.log("No jobs."); return; }
  console.log(JSON.stringify({ jobId: job.id, kind: job.kind, status: job.status }, null, 2));
}

function cmdResult(argv) {
  const o = parseFlags(argv, { bools: [], vals: [] });
  const job = o._[0] ? readJob(o._[0]) : latestJob();
  if (!job) { console.log("No jobs."); return; }
  if (job.status === "running") { console.log(`Job ${job.id} still running.`); return; }
  process.stdout.write(job.output || "(no output)\n");
}

// config: show, set model, or clear.
//   config                       -> print current config + resolved model
//   config set-model "<name>"    -> persist default model
//   config clear-model           -> remove saved default
function cmdConfig(argv) {
  const [action, ...rest] = argv;
  if (action === "set-model") {
    const name = rest.join(" ").trim();
    if (!name) { console.error("config set-model: model name required"); process.exit(2); }
    const cfg = readConfig(); cfg.model = name; writeConfig(cfg);
    console.log(`Saved default model: ${name}`);
    return;
  }
  if (action === "clear-model") {
    const cfg = readConfig(); delete cfg.model; writeConfig(cfg);
    console.log("Cleared default model.");
    return;
  }
  console.log(JSON.stringify({ config: readConfig(), resolvedModel: resolveModel() || "(agy default)" }, null, 2));
}

// hint: live cheat-sheet — current model, available agy models, commands, permission modes.
function cmdHint() {
  let models = "(run `agy models` to list)";
  try { models = execFileSync(AGY_BIN, ["models"], { encoding: "utf8" }).trim(); } catch {}
  const active = resolveModel();
  const cfg = readConfig();
  const src = process.env.AGY_MODEL ? "AGY_MODEL env"
    : cfg.model ? "saved config" : "built-in default";
  console.log(`agy — Google Antigravity from Claude Code

Active model : ${active}   (from ${src})
Available    :
${models.split("\n").map(m => "  " + m).join("\n")}

Commands:
  task [--background] [--write] [--yolo] [--resume] [--model "<name>"] "<prompt>"
  review / adversarial-review [--base <ref>] [--scope auto|working-tree|branch]
  status [--all] | result [job-id]
  config set-model "<name>" | clear-model
  hint

Permissions: (default) read-only · --write edits+prompt · --yolo bypass prompts
Model set   : --model persists as new default; precedence flag > env > config > default
Tip         : model name must match "agy models" exactly, quotes included.`);
}

function parseFlags(argv, { bools, vals }) {
  const o = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const name = a.slice(2);
      if (bools.includes(name)) o[name] = true;
      else if (vals.includes(name)) o[name] = argv[++i];
      else o._.push(a);
    } else o._.push(a);
  }
  return o;
}

async function main() {
  const [sub, ...rest] = process.argv.slice(2);
  switch (sub) {
    case "task": return cmdTask(rest);
    case "review": return cmdReview(rest, false);
    case "adversarial-review": return cmdReview(rest, true);
    case "status": return cmdStatus(rest);
    case "result": return cmdResult(rest);
    case "config": return cmdConfig(rest);
    case "hint": return cmdHint();
    case "_worker": return cmdWorker(rest);
    case undefined: case "help": case "--help": return usage();
    default: console.error("unknown subcommand: " + sub); usage(); process.exit(2);
  }
}
main().catch(e => { console.error(e); process.exit(1); });
