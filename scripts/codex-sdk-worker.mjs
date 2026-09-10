import readline from "node:readline";
import { Codex } from "@openai/codex-sdk";

const request = await readRequest();
const controller = new AbortController();
let interrupted = false;

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  if (line.trim() === "interrupt") {
    interrupted = true;
    controller.abort();
  }
});

function emit(event) {
  process.stdout.write(`${JSON.stringify(event)}\n`);
}

try {
  const codex = new Codex({
    codexPathOverride: request.codexPath,
    env: request.env,
    configOverrides: request.configOverrides
  });
  const thread = codex.resumeThread(request.threadId, {
    model: request.model,
    modelReasoningEffort: request.effort,
    sandboxMode: request.sandbox,
    workingDirectory: request.cwd,
    skipGitRepoCheck: true,
    networkAccessEnabled: request.networkAccessEnabled === true,
    approvalPolicy: request.approvalPolicy,
    additionalDirectories: request.additionalDirectories || []
  });
  const streamed = await thread.runStreamed(request.prompt, {
    outputSchema: request.outputSchema,
    signal: controller.signal
  });
  for await (const event of streamed.events) {
    emit(event);
    if (event.type === "thread.started") {
      emit({ type: "dispatch.authoritative", thread_id: event.thread_id });
    }
    if (event.type === "turn.started") {
      emit({ type: "dispatch.turn_started" });
    }
  }
} catch (error) {
  const message = String(error?.message || error);
  const codeMatch = message.match(/code\s+(-?\d+)/i);
  emit({
    type: interrupted ? "dispatch.interrupted" : "dispatch.error",
    code: error?.code ?? (codeMatch ? Number(codeMatch[1]) : null),
    message
  });
  process.exitCode = interrupted ? 0 : 1;
} finally {
  input.close();
}

function readRequest() {
  return new Promise((resolve, reject) => {
    let settled = false;
    const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    input.once("line", (line) => {
      settled = true;
      input.close();
      try {
        resolve(JSON.parse(line));
      } catch (error) {
        reject(new Error(`invalid worker request: ${error.message}`));
      }
    });
    input.once("close", () => {
      if (!settled) reject(new Error("worker request missing"));
    });
  });
}
