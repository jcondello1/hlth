import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import { BedrockAgentRuntimeClient, InvokeAgentCommand } from "@aws-sdk/client-bedrock-agent-runtime";
import { fromIni } from "@aws-sdk/credential-providers";

import { webcrypto as crypto } from "crypto";
if (!globalThis.crypto) globalThis.crypto = crypto;


dotenv.config();
console.log("Agent wiring:", process.env.AGENT_ID, process.env.AGENT_ALIAS_ID);


const app = express();
app.use(cors());
app.use(express.json());

app.use(express.static("public"));           // serve files under /public
app.get("/", (_req, res) => res.sendFile(process.cwd() + "/public/index.html"));


const {
  AWS_REGION,
  AGENT_ID,
  AGENT_ALIAS_ID,
} = process.env;

if (!AWS_REGION || !AGENT_ID || !AGENT_ALIAS_ID) {
  console.log("Using config:", {
	region: AWS_REGION,
	agentId: AGENT_ID,
	agentAliasId: AGENT_ALIAS_ID,
  });
  console.error("Missing env: AWS_REGION, AGENT_ID, AGENT_ALIAS_ID");
  process.exit(1);
}

// Use instance role in production (Elastic Beanstalk), local profile in dev
const credentials =
  process.env.NODE_ENV === "production"
    ? undefined
    : fromIni({ profile: process.env.AWS_PROFILE || "root" });

const client = new BedrockAgentRuntimeClient({
  region: AWS_REGION,
  credentials, // undefined in prod = use instance role
});


// Robust stream reader for Bedrock Agent Runtime event streams
async function readStreamToText(stream) {
  let finalText = "";
  let buffer = "";

  // Helper: pull a Uint8Array/string out of various event shapes
  const asText = (evt) => {
    // Common shapes we might see:
    //  - Uint8Array
    //  - { bytes: Uint8Array }
    //  - { chunk: Uint8Array } or { chunk: { bytes: Uint8Array } }
    //  - string
    let u8 =
      (evt && evt.bytes) ||
      (evt && evt.chunk && evt.chunk.bytes) ||
      (evt && evt.chunk) ||
      (ArrayBuffer.isView(evt) ? evt : null);

    if (typeof evt === "string") return evt;
    if (typeof u8 === "string") return u8;
    if (u8 instanceof Uint8Array) return Buffer.from(u8).toString("utf8");
    if (u8 && ArrayBuffer.isView(u8)) return Buffer.from(u8.buffer).toString("utf8");
    return "";
  };

  for await (const evt of stream) {
    const piece = asText(evt);
    if (!piece) continue;
    buffer += piece;

    // Bedrock usually newline-delimits JSON events.
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;

      try {
        const obj = JSON.parse(line);

        // Typical fields that carry model text
        if (typeof obj.outputText === "string") {
          finalText += obj.outputText;
        } else if (Array.isArray(obj.content)) {
          for (const c of obj.content) {
            if (typeof c.text === "string") finalText += c.text;
          }
        } else if (typeof obj.text === "string") {
          finalText += obj.text;
        }
      } catch {
        // Not JSON? Just append raw text.
        finalText += line;
      }
    }
  }

  if (buffer.trim()) finalText += buffer.trim();
  return finalText.trim();
}


// POST /ask  { inputText: string, sessionId?: string }
app.post("/ask", async (req, res) => {
  try {
    const inputText = String(req.body?.inputText ?? "").trim();
    if (!inputText) return res.status(400).json({ error: "Missing inputText" });

    // const client = new BedrockAgentRuntimeClient({ region: process.env.AWS_REGION || "us-east-1" });
    const cmd = new InvokeAgentCommand({
      agentId: process.env.AGENT_ID,
      agentAliasId: process.env.AGENT_ALIAS_ID,
      sessionId: "webui-session-1",
      inputText,
      enableTrace: true, // <-- important
    });

    const resp = await client.send(cmd);

    let finalText = "";
    let failure = null;
    let traceNotes = [];

    for await (const event of resp.completion) {
      const type = Object.keys(event)[0];
      const payload = event[type];

      // Log everything to the terminal where you run `npm start`
      console.log("[AgentStream]", type, JSON.stringify(payload, null, 2));

      if (type === "chunk") {
        finalText += Buffer.from(payload.bytes).toString("utf-8");
      } else if (type === "failed") {
        failure = payload?.error?.message || "Agent failed";
      } else if (type === "error") {
        failure = payload?.message || "Agent error";
      } else if (type === "trace") {
        // Collect human-readable trace notes, if any
        const msg = payload?.trace?.message || payload?.trace?.observation?.content || null;
        if (msg) traceNotes.push(msg);
      }
    }

    if (failure) {
      // Show exactly what the Agent complained about
      return res.status(502).json({
        error: failure,
        trace: traceNotes,
        text: finalText
      });
    }

    // Friendly fallback if the model returns no assistant text
    if (!finalText.trim()) {
      finalText = "✅ Health log updated.";
    }

    res.json({ text: finalText });
  } catch (err) {
    console.error("[InvokeAgent error]", err);
    res.status(500).json({ error: err?.message || "InvokeAgent failed" });
  }
});




const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Bedrock proxy listening on http://localhost:${PORT}`);
});
