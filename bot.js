require("dotenv").config();
const { Client, GatewayIntentBits, Partials } = require("discord.js");
const Anthropic = require("@anthropic-ai/sdk");
const fs = require("fs");
const path = require("path");
const { exec } = require("child_process");

// ── Config ──────────────────────────────────────────────────────────
const DISCORD_TOKEN = process.env.DISCORD_TOKEN;
const ANTHROPIC_API_KEY = process.env.ANTHROPIC_API_KEY;
const PROJECT_PATH = process.env.PROJECT_PATH;
const ALLOWED_USER_ID = process.env.ALLOWED_USER_ID || null;

if (!DISCORD_TOKEN || !ANTHROPIC_API_KEY || !PROJECT_PATH) {
  console.error("Missing required env vars. Copy .env.example to .env and fill it in.");
  process.exit(1);
}

if (!fs.existsSync(PROJECT_PATH)) {
  console.error(`PROJECT_PATH does not exist: ${PROJECT_PATH}`);
  process.exit(1);
}

// ── Clients ─────────────────────────────────────────────────────────
const discord = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
  ],
  partials: [Partials.Channel],
});

const anthropic = new Anthropic({ apiKey: ANTHROPIC_API_KEY });

// ── State (per-channel) ─────────────────────────────────────────────
const sessions = new Map();

function getSession(channelId) {
  if (!sessions.has(channelId)) {
    sessions.set(channelId, { plan: null, history: [] });
  }
  return sessions.get(channelId);
}

// ── Claude helpers ──────────────────────────────────────────────────
const SYSTEM_PROMPT = `You are a senior software engineer acting as a remote coding assistant. You work inside a real project directory on the user's PC.

Project path: ${PROJECT_PATH}

You operate in two modes:

MODE 1 — PLAN (triggered by !build)
Return ONLY a plain-English plan. Number each step. For each step say:
- What file will be created or modified (relative path)
- What it will contain / what changes
- Why

Do NOT include any code. Keep it readable on a phone screen.
End with: "Reply !go to execute, or !edit <changes> to adjust."

MODE 2 — EXECUTE (triggered by !go)
Return the actual code to write. Format your response as a series of FILE BLOCKS:

===FILE: relative/path/to/file.js===
<full file contents>
===END FILE===

===FILE: another/file.js===
<full file contents>
===END FILE===

Rules:
- Every file block must have the EXACT markers above (===FILE: and ===END FILE===)
- Use relative paths from the project root
- Include complete file contents, not diffs or partial snippets
- If you need to run a shell command (like npm install), add a block:

===RUN: npm install some-package===

For !ask questions, just answer normally — no file blocks, no plan format.`;

async function callClaude(session, userMessage) {
  session.history.push({ role: "user", content: userMessage });

  // Keep history manageable (last 20 messages)
  if (session.history.length > 20) {
    session.history = session.history.slice(-20);
  }

  const response = await anthropic.messages.create({
    model: "claude-sonnet-4-20250514",
    max_tokens: 16000,
    system: SYSTEM_PROMPT,
    messages: session.history,
  });

  const text = response.content[0].text;
  session.history.push({ role: "assistant", content: text });
  return text;
}

// ── File writing ────────────────────────────────────────────────────
function parseAndWriteFiles(response) {
  const fileRegex = /===FILE:\s*(.+?)===\n([\s\S]*?)===END FILE===/g;
  const runRegex = /===RUN:\s*(.+?)===/g;
  const filesWritten = [];
  const commands = [];

  let match;
  while ((match = fileRegex.exec(response)) !== null) {
    const relPath = match[1].trim();
    const contents = match[2];
    const fullPath = path.join(PROJECT_PATH, relPath);

    const dir = path.dirname(fullPath);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(fullPath, contents, "utf-8");
    filesWritten.push(relPath);
  }

  while ((match = runRegex.exec(response)) !== null) {
    commands.push(match[1].trim());
  }

  return { filesWritten, commands };
}

function runShellCommand(cmd) {
  return new Promise((resolve) => {
    exec(cmd, { cwd: PROJECT_PATH, timeout: 60000 }, (err, stdout, stderr) => {
      if (err) {
        resolve(`Error: ${err.message}\n${stderr}`);
      } else {
        resolve(stdout || stderr || "(no output)");
      }
    });
  });
}

// ── Discord message splitting ───────────────────────────────────────
async function sendLong(channel, text) {
  const MAX = 1950;
  if (text.length <= MAX) {
    await channel.send(text);
    return;
  }

  const lines = text.split("\n");
  let chunk = "";
  for (const line of lines) {
    if (chunk.length + line.length + 1 > MAX) {
      await channel.send(chunk);
      chunk = "";
    }
    chunk += line + "\n";
  }
  if (chunk.trim()) {
    await channel.send(chunk);
  }
}

// ── Command router ──────────────────────────────────────────────────
discord.on("messageCreate", async (msg) => {
  if (msg.author.bot) return;
  if (ALLOWED_USER_ID && msg.author.id !== ALLOWED_USER_ID) return;

  const content = msg.content.trim();
  if (!content.startsWith("!")) return;

  const session = getSession(msg.channel.id);

  try {
    // ── !build <description> ──────────────────────────────────────
    if (content.startsWith("!build ")) {
      const description = content.slice(7);
      await msg.react("🧠");

      const prompt = `Create a PLAN (no code) for: ${description}`;
      const plan = await callClaude(session, prompt);
      session.plan = plan;

      await sendLong(msg.channel, `📋 **Plan:**\n\n${plan}`);
    }

    // ── !go ───────────────────────────────────────────────────────
    else if (content === "!go") {
      if (!session.plan) {
        await msg.reply("No plan to execute. Use `!build <description>` first.");
        return;
      }
      await msg.react("⚡");

      const prompt = "Execute the plan now. Output all files using the ===FILE: format.";
      const response = await callClaude(session, prompt);
      const { filesWritten, commands } = parseAndWriteFiles(response);

      let summary = "";
      if (filesWritten.length > 0) {
        summary += `✅ **Files written:**\n${filesWritten.map((f) => `• \`${f}\``).join("\n")}\n\n`;
      } else {
        summary += "⚠️ No files were parsed from the response.\n\n";
      }

      // Run any shell commands
      for (const cmd of commands) {
        summary += `🔧 Running: \`${cmd}\`\n`;
        const output = await runShellCommand(cmd);
        summary += `\`\`\`\n${output.slice(0, 500)}\n\`\`\`\n`;
      }

      summary += "Cursor should auto-reload. Check your editor!";
      session.plan = null;
      await sendLong(msg.channel, summary);
    }

    // ── !edit <changes> ───────────────────────────────────────────
    else if (content.startsWith("!edit ")) {
      if (!session.plan) {
        await msg.reply("No plan to edit. Use `!build <description>` first.");
        return;
      }
      const changes = content.slice(6);
      await msg.react("✏️");

      const prompt = `Revise the plan with these changes: ${changes}\n\nReturn the updated PLAN (no code).`;
      const plan = await callClaude(session, prompt);
      session.plan = plan;

      await sendLong(msg.channel, `📋 **Revised Plan:**\n\n${plan}`);
    }

    // ── !ask <question> ───────────────────────────────────────────
    else if (content.startsWith("!ask ")) {
      const question = content.slice(5);
      await msg.react("💬");

      const answer = await callClaude(session, question);
      await sendLong(msg.channel, answer);
    }

    // ── !run <command> ────────────────────────────────────────────
    else if (content.startsWith("!run ")) {
      const cmd = content.slice(5);
      await msg.react("🔧");

      const output = await runShellCommand(cmd);
      await sendLong(msg.channel, `\`\`\`\n${output.slice(0, 1900)}\n\`\`\``);
    }

    // ── !status ───────────────────────────────────────────────────
    else if (content === "!status") {
      const hasPlan = session.plan ? "Yes" : "No";
      const historyLen = session.history.length;
      await msg.reply(
        `🤖 **Bot Status**\nProject: \`${PROJECT_PATH}\`\nActive plan: ${hasPlan}\nConversation messages: ${historyLen}`
      );
    }

    // ── !clear ────────────────────────────────────────────────────
    else if (content === "!clear") {
      session.plan = null;
      session.history = [];
      await msg.reply("🗑️ Session cleared.");
    }

    // ── !help ─────────────────────────────────────────────────────
    else if (content === "!help") {
      await msg.reply(
        `**Commands:**
\`!build <description>\` — Get a plan (no code yet)
\`!go\` — Execute the current plan (writes files)
\`!edit <changes>\` — Revise the plan before executing
\`!ask <question>\` — Ask Claude anything (no files written)
\`!run <command>\` — Run a shell command on your PC
\`!status\` — Check bot status
\`!clear\` — Reset conversation & plan
\`!help\` — Show this message`
      );
    }
  } catch (err) {
    console.error("Error:", err);
    await msg.reply(`❌ Error: ${err.message?.slice(0, 500) || "Unknown error"}`);
  }
});

// ── Start ───────────────────────────────────────────────────────────
discord.once("ready", () => {
  console.log(`Bot online as ${discord.user.tag}`);
  console.log(`Watching project: ${PROJECT_PATH}`);
  console.log(`Allowed user: ${ALLOWED_USER_ID || "anyone"}`);
  console.log("Commands: !build, !go, !edit, !ask, !run, !status, !clear, !help");
});

discord.login(DISCORD_TOKEN);
