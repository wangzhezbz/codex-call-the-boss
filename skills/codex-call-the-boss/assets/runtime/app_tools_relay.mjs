import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import os from "node:os";

const MAX_FRAME_BYTES = 8 * 1024 * 1024;
const nativePipePath = (process.env.CODEX_APP_TOOLS_PIPE_PATH ?? "").trim();
const stdioMode = process.argv[2] === "--stdio";
const relaySocketPath = stdioMode ? "" : path.resolve(process.argv[2] ?? "");
const stateDirectory = path.resolve(process.env.CODEX_PHONE_STATE_DIR ??
  (stdioMode ? path.join(os.homedir(), ".codex-phone") : path.dirname(relaySocketPath)));
const lifecyclePath = stdioMode ? "" : path.join(path.dirname(relaySocketPath), "app-tools-relay-lifecycle.jsonl");
function lifecycle(event, detail = {}) {
  if (!lifecyclePath) return;
  try {
    fs.appendFileSync(lifecyclePath, JSON.stringify({ at: new Date().toISOString(),
      pid: process.pid, parentPid: process.ppid, event, ...detail }) + "\n", { mode: 0o600 });
  } catch { /* logging must not change transport behavior */ }
}

// Observe fatal exceptions without intercepting Node's normal fatal exit.
// No message/stack/request body: those can contain private tool arguments.
process.on("uncaughtExceptionMonitor", (error, origin) => lifecycle("uncaught_exception", {
  origin, errorName: error?.name ?? "Error", errorCode: error?.code ?? null,
}));
process.once("exit", code => lifecycle("exit", { code }));

if (!nativePipePath || (!stdioMode && !relaySocketPath)) {
  throw new Error("missing app-tools pipe or relay socket path");
}

class NativeAppToolsClient {
  constructor(pipePath) {
    this.pipePath = pipePath;
    this.socket = null;
    this.connecting = null;
    this.nextId = 1;
    this.pending = new Map();
    this.pendingData = Buffer.alloc(0);
    this.tools = null;
  }

  async connect() {
    if (this.socket && !this.socket.destroyed) return;
    if (this.connecting) return this.connecting;
    this.connecting = new Promise((resolve, reject) => {
      const socket = net.createConnection(this.pipePath);
      const timer = setTimeout(() => fail(new Error("Codex app tools pipe connection timed out")), 2500);
      const fail = (error) => {
        clearTimeout(timer);
        socket.destroy();
        reject(error);
      };
      socket.once("error", fail);
      socket.once("connect", () => {
        clearTimeout(timer);
        socket.off("error", fail);
        this.socket = socket;
        this.connecting = null;
        socket.on("data", (chunk) => this.onData(socket, chunk));
        socket.on("error", (error) => this.onDisconnect(socket, error));
        socket.on("close", () =>
          this.onDisconnect(socket, new Error("Codex app tools pipe closed")),
        );
        resolve();
      });
    }).catch((error) => {
      this.connecting = null;
      throw error;
    });
    return this.connecting;
  }

  async request(method, params, timeoutMs = 30_000) {
    // Validate before allocating a pending promise: oversized frames must
    // not leave an unobserved rejection behind after the caller already failed.
    const id = this.nextId++;
    const payload = Buffer.from(JSON.stringify({ id, jsonrpc: "2.0", method, params }), "utf8");
    if (payload.length > MAX_FRAME_BYTES) throw new Error("request is too large");
    await this.connect();
    const socket = this.socket;
    if (!socket) throw new Error("Codex app tools pipe is unavailable");
    const response = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Codex app tool timed out: ${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (error) => {
          clearTimeout(timer);
          reject(error);
        },
      });
    });
    const frame = Buffer.alloc(4 + payload.length);
    frame.writeUInt32LE(payload.length, 0);
    payload.copy(frame, 4);
    try { socket.write(frame); } catch (error) {
      this.pending.get(id)?.reject(error);
      this.pending.delete(id);
    }
    return response;
  }

  onData(socket, chunk) {
    if (this.socket !== socket) return;
    this.pendingData = Buffer.concat([this.pendingData, chunk]);
    while (this.pendingData.length >= 4) {
      const size = this.pendingData.readUInt32LE(0);
      if (size > MAX_FRAME_BYTES) {
        socket.destroy(new Error("response is too large"));
        return;
      }
      if (this.pendingData.length < 4 + size) return;
      const payload = this.pendingData.subarray(4, 4 + size);
      this.pendingData = this.pendingData.subarray(4 + size);
      let response;
      try {
        response = JSON.parse(payload.toString("utf8"));
      } catch {
        socket.destroy(new Error("invalid Codex app tools response"));
        return;
      }
      if (!response || typeof response !== "object" || Array.isArray(response)) {
        socket.destroy(new Error("invalid Codex app tools response"));
        return;
      }
      const pending = this.pending.get(Number(response.id));
      if (!pending) continue;
      this.pending.delete(Number(response.id));
      if (response.error) {
        pending.reject(new Error(response.error.message ?? "Codex app tool failed"));
      } else {
        pending.resolve(response.result);
      }
    }
  }

  onDisconnect(socket, error) {
    if (this.socket !== socket) return;
    this.socket = null;
    this.pendingData = Buffer.alloc(0);
    this.tools = null;
    lifecycle("native_pipe_disconnected");
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }

  async tool(name) {
    if (!this.tools) {
      const result = await this.request("tools/list", { threadStartKind: "all" });
      this.tools = new Map((result.tools ?? []).map((tool) => [tool.name, tool]));
    }
    const tool = this.tools.get(name);
    if (!tool) throw new Error(`Codex app tool is unavailable: ${name}`);
    return tool;
  }

  async callTool(name, argumentsValue, callerThreadId) {
    const tool = await this.tool(name);
    // A subscription may be revoked while tools/list is in flight.
    authorizeTarget(argumentsValue.threadId, name === "send_message_to_thread" ? callerThreadId : null);
    const callId = `phone-${crypto.randomUUID()}`;
    const result = await this.request(
      "tools/call",
      {
        arguments: argumentsValue,
        callId,
        namespace: tool.namespace,
        threadId: callerThreadId,
        tool: name,
        turnId: callId,
      },
      120_000,
    );
    if (!result.success) {
      const detail = (result.contentItems ?? [])
        .filter((item) => item.type === "inputText")
        .map((item) => item.text)
        .join(" ");
      throw new Error(detail || `${name} failed`);
    }
    return result;
  }

  close() {
    if (this.socket && !this.socket.destroyed) this.socket.destroy();
    this.socket = null;
  }
}

function validateThreadId(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{8,128}$/.test(value)) {
    throw new Error("invalid thread id");
  }
  return value;
}

function validatePrompt(value) {
  if (typeof value !== "string" || !value.trim() || value.length > 10_000) {
    throw new Error("invalid phone task prompt");
  }
  return value.trim();
}

function authorizationState(name) {
  try {
    const filename = path.join(stateDirectory, name);
    if (fs.statSync(filename).size > 256 * 1024) throw new Error();
    const record = JSON.parse(fs.readFileSync(filename, "utf8"));
    if (!record || Array.isArray(record) || typeof record !== "object") throw new Error();
    return record;
  } catch { throw new Error("phone authorization state unavailable"); }
}

function authorizeTarget(threadId, callerThreadId = null) {
  const config = authorizationState("config.json");
  const registry = authorizationState("sessions.json");
  const record = registry.sessions?.[threadId];
  const fixedCaller = config.relay_caller_thread_id;
  if (config.enabled !== true || record?.enabled !== true || record?.thread_id !== threadId ||
      threadId === fixedCaller || (callerThreadId !== null && callerThreadId !== fixedCaller)) {
    throw new Error("phone source or target is not authorized");
  }
}

const nativeClient = new NativeAppToolsClient(nativePipePath);

async function dispatch(message) {
  const params = message.params ?? {};
  if (message.method === "health") {
    // A cached tool name is not evidence that the desktop is still reachable.
    const catalog = await nativeClient.request("tools/list", { threadStartKind: "all" }, 2500);
    if (!Array.isArray(catalog?.tools) ||
        !["read_thread", "send_message_to_thread"].every(name =>
          catalog.tools.some(tool => tool?.name === name))) {
      throw new Error("Codex desktop command transport is unavailable");
    }
    return { ready: true };
  }
  if (message.method === "read_thread") {
    const threadId = validateThreadId(params.threadId);
    authorizeTarget(threadId);
    return nativeClient.callTool(
      "read_thread",
      { threadId, turnLimit: 1, includeOutputs: false },
      threadId,
    );
  }
  if (message.method === "read_context") {
    const threadId = validateThreadId(params.threadId);
    authorizeTarget(threadId);
    const result = await nativeClient.callTool("read_thread",
      { threadId, turnLimit: 6, includeOutputs: false }, threadId);
    for (const item of result.contentItems ?? []) {
      if (item.type !== "inputText") continue;
      let data;
      try { data = JSON.parse(item.text); } catch { continue; }
      const messages = [];
      for (const turn of [...(data.turns ?? [])].reverse()) {
        for (const entry of turn.items ?? []) {
          if (entry.type === "userMessage") {
            const text = (entry.content ?? []).filter(p => p.type === "text").map(p => p.text ?? "").join("");
            messages.push(`user: ${text.slice(0, 600)}`);
          } else if (entry.type === "agentMessage" && ["final", "final_answer"].includes(entry.phase)) {
            messages.push(`assistant: ${(entry.text ?? "").slice(0, 900)}`);
          }
        }
      }
      // No reasoning, commands, tool output, or binary content.
      return { sourceThreadId: threadId, text: messages.slice(-8).join("\n").slice(-3200) };
    }
    return { sourceThreadId: threadId, text: "" };
  }
  if (message.method === "send_message_to_thread") {
    const callerThreadId = validateThreadId(params.callerThreadId);
    const threadId = validateThreadId(params.threadId);
    const prompt = validatePrompt(params.prompt);
    authorizeTarget(threadId, callerThreadId);
    return nativeClient.callTool(
      "send_message_to_thread",
      { threadId, prompt },
      callerThreadId,
    );
  }
  throw new Error("unsupported relay method");
}

async function runStdio() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk.toString("utf8");
  let request;
  try {
    request = JSON.parse(input);
    const result = await dispatch(request);
    process.stdout.write(JSON.stringify({ result }));
  } catch (error) {
    process.stdout.write(
      JSON.stringify({
        error: error instanceof Error ? error.message : String(error),
      }),
    );
  } finally {
    nativeClient.close();
  }
}

if (stdioMode) {
  await runStdio();
  process.exit(0);
}

// A listening socket may belong to an unhealthy old relay or another
// process. Only a correlated, fresh health response permits reuse. Never
// replace a live but unhealthy listener or replay any pending command.
async function probeExistingRelay(socketPath) {
  return new Promise(resolve => {
    const probe = net.createConnection(socketPath);
    const id = `health-${crypto.randomUUID()}`;
    let connected = false, settled = false, buffer = "";
    const timer = setTimeout(() => finish("unavailable"), 5500);
    function finish(state) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      probe.destroy();
      resolve(state);
    }
    probe.setEncoding("utf8");
    probe.once("connect", () => {
      connected = true;
      probe.write(JSON.stringify({ id, method: "health", params: {} }) + "\n");
    });
    probe.on("data", chunk => {
      buffer += chunk;
      if (Buffer.byteLength(buffer) > 4096) return finish("unavailable");
      const end = buffer.indexOf("\n");
      if (end < 0) return;
      try {
        const response = JSON.parse(buffer.slice(0, end));
        finish(response?.id === id && !response.error && response.result?.ready === true
          ? "ready" : "unavailable");
      } catch { finish("unavailable"); }
    });
    probe.once("error", error => finish(!connected &&
      ["ECONNREFUSED", "ENOENT"].includes(error.code) ? "stale" : "unavailable"));
    probe.once("close", () => finish("unavailable"));
  });
}

if (fs.existsSync(relaySocketPath)) {
  const stat = fs.lstatSync(relaySocketPath);
  if (!stat.isSocket()) throw new Error("relay path exists and is not a socket");
  const existing = await probeExistingRelay(relaySocketPath);
  if (existing === "ready") {
    process.stdout.write("Codex app tools relay already running\n");
    process.exit(0);
  }
  if (existing !== "stale") {
    lifecycle("startup_failed", { reason: "existing_relay_unavailable" });
    process.stderr.write("Existing Codex relay failed its health check; instance preserved. Inspect it before restarting.\n");
    process.exit(1);
  }
  // Do not unlink a different socket installed during the bounded probe.
  const current = fs.lstatSync(relaySocketPath);
  if (!current.isSocket() || current.dev !== stat.dev || current.ino !== stat.ino) {
    throw new Error("relay socket changed during startup; preserved");
  }
  fs.unlinkSync(relaySocketPath);
}

try {
  await dispatch({ method: "health" });
} catch {
  lifecycle("startup_failed", { reason: "native_health_unavailable" });
  nativeClient.close();
  process.stderr.write("Codex desktop health check failed; relay was not started.\n");
  process.exit(1);
}

const clients = new Set();
const server = net.createServer((socket) => {
  clients.add(socket);
  socket.once("close", () => clients.delete(socket));
  socket.on("error", () => {});
  socket.setEncoding("utf8");
  let buffer = "";
  socket.on("data", (chunk) => {
    if (shuttingDown) { socket.destroy(); return; }
    buffer += chunk;
    if (Buffer.byteLength(buffer) > MAX_FRAME_BYTES) {
      socket.destroy();
      return;
    }
    while (true) {
      const index = buffer.indexOf("\n");
      if (index < 0) break;
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      void (async () => {
        let request;
        try {
          request = JSON.parse(line);
          const result = await dispatch(request);
          if (!socket.destroyed) socket.write(`${JSON.stringify({ id: request.id, result })}\n`);
        } catch (error) {
          if (!socket.destroyed) socket.write(
            `${JSON.stringify({
              id: request?.id ?? null,
              error: error instanceof Error ? error.message : String(error),
            })}\n`,
          );
        }
      })();
    }
  });
});

let shuttingDown = false;
let ownedSocket = null;
function cleanup(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  lifecycle("stopping", { signal });
  // An abandoned local reader must not hold an idle update open forever.
  // Give accepted work a bounded drain, then close connections without
  // retrying sends or representing an uncertain result as delivered.
  const deadline = setTimeout(() => {
    lifecycle("shutdown_drain_timeout", { openClients: clients.size });
    for (const socket of clients) socket.destroy();
  }, 1500);
  deadline.unref();
  server.close(() => {
    clearTimeout(deadline);
    nativeClient.close();
    try {
      const stat = fs.lstatSync(relaySocketPath);
      if (stat.isSocket() && stat.dev === ownedSocket?.dev && stat.ino === ownedSocket?.ino) {
        fs.unlinkSync(relaySocketPath);
      }
    } catch { /* Never remove a replacement path or prevent exit. */ }
    process.exit(0);
  });
}

server.once("error", () => {
  lifecycle("startup_failed", { reason: "relay_listen_failed" });
  nativeClient.close();
  process.stderr.write("Codex relay could not listen; existing socket was not replaced.\n");
  process.exit(1);
});
server.listen(relaySocketPath, () => {
  ownedSocket = fs.lstatSync(relaySocketPath);
  fs.chmodSync(relaySocketPath, 0o600);
  process.stdout.write("Codex app tools relay ready\n");
  lifecycle("ready", { tty: Boolean(process.stdin.isTTY) });
});

process.once("SIGINT", () => cleanup("SIGINT"));
process.once("SIGTERM", () => cleanup("SIGTERM"));
process.once("SIGHUP", () => cleanup("SIGHUP"));
