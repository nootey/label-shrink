import { Client, GatewayIntentBits, AttachmentBuilder } from 'discord.js';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';
import 'dotenv/config';

const run = promisify(execFile);

const MAX_PDF_BYTES = 20 * 1024 * 1024;
const FETCH_TIMEOUT_MS = 15_000;

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
  ],
});

client.once('clientReady', () => console.log(`up as ${client.user.tag}`));

client.on('messageCreate', async (msg) => {
  if (msg.author.bot) return;

  const pdfAttachment = msg.attachments.find(a => a.name?.toLowerCase().endsWith('.pdf'));

  let name, buffer;
  if (pdfAttachment) {
    name = pdfAttachment.name;
    const res = await fetch(pdfAttachment.url);
    buffer = Buffer.from(await res.arrayBuffer());
  } else {
    const linked = await findLinkedPdf(msg.content);
    if (!linked) return;
    ({ name, buffer } = linked);
  }

  const dir = await mkdtemp(join(tmpdir(), 'a4-'));
  try {
    const inPath  = join(dir, 'in.pdf');
    const outPath = join(dir, 'out.pdf');

    await writeFile(inPath, buffer);
    await run('bash', ['./pdf-to-a4.sh', inPath, outPath], { timeout: 30_000 });

    await msg.reply({
      files: [new AttachmentBuilder(outPath, {
        name: name.replace(/\.pdf$/i, '-a4.pdf'),
      })],
    });
  } catch (err) {
    console.error(err);
    await msg.reply(`Couldn't convert that: ${err.stderr?.trim() ?? err.message}`);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

client.login(process.env.DISCORD_TOKEN);

// ---- link scanning: find a hosted PDF among the links in a message --------

function extractUrls(text) {
  return text.match(/https?:\/\/[^\s<>()"']+/gi) ?? [];
}

async function findLinkedPdf(content) {
  for (const candidate of extractUrls(content)) {
    let url;
    try {
      url = new URL(candidate);
    } catch {
      continue;
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') continue;
    if (await isBlockedHost(url.hostname)) continue;

    let res;
    try {
      res = await fetch(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    } catch {
      continue;
    }
    if (!res.ok || !looksLikePdf(res, candidate)) {
      res.body?.cancel();
      continue;
    }

    try {
      const buffer = await readBodyWithLimit(res, MAX_PDF_BYTES);
      return { buffer, name: filenameFromResponse(res, candidate) };
    } catch (err) {
      console.warn(`skipping ${candidate}: ${err.message}`);
    }
  }
  return null;
}

function looksLikePdf(res, url) {
  const type = res.headers.get('content-type') ?? '';
  if (type.toLowerCase().includes('application/pdf')) return true;

  const disposition = res.headers.get('content-disposition') ?? '';
  if (/\.pdf(["'\s;]|$)/i.test(disposition)) return true;

  try {
    if (/\.pdf$/i.test(new URL(url).pathname)) return true;
  } catch {
    // ignore
  }
  return false;
}

function filenameFromResponse(res, url) {
  const disposition = res.headers.get('content-disposition') ?? '';

  const starMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (starMatch) return decodeURIComponent(starMatch[1]).replace(/["']/g, '');

  const match = disposition.match(/filename="?([^";]+)"?/i);
  if (match) return match[1];

  try {
    const base = new URL(url).pathname.split('/').pop();
    if (base) return decodeURIComponent(base);
  } catch {
    // ignore
  }
  return 'label.pdf';
}

async function readBodyWithLimit(res, maxBytes) {
  const reader = res.body.getReader();
  const chunks = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      throw new Error(`file too large (over ${Math.floor(maxBytes / 1024 / 1024)} MB)`);
    }
    chunks.push(value);
  }
  return Buffer.concat(chunks);
}

// Links come from message text, not from a trusted source, so refuse to let
// the bot's server fetch its own internal/private network.
async function isBlockedHost(hostname) {
  if (isIP(hostname)) {
    return isIP(hostname) === 4 ? isPrivateIPv4(hostname) : isPrivateIPv6(hostname);
  }
  if (hostname.toLowerCase() === 'localhost') return true;

  let addresses;
  try {
    addresses = await lookup(hostname, { all: true });
  } catch {
    return true;
  }
  return addresses.some(({ address, family }) =>
    family === 4 ? isPrivateIPv4(address) : isPrivateIPv6(address)
  );
}

function isPrivateIPv4(ip) {
  const parts = ip.split('.').map(Number);
  if (parts.length !== 4 || parts.some(n => Number.isNaN(n))) return true;
  const [a, b] = parts;
  if (a === 10 || a === 127 || a === 0) return true;
  if (a === 169 && b === 254) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 100 && b >= 64 && b <= 127) return true; // carrier-grade NAT
  if (a >= 224) return true; // multicast / reserved
  return false;
}

function isPrivateIPv6(ip) {
  const lower = ip.toLowerCase();
  if (lower === '::1') return true;
  if (lower.startsWith('fe80:')) return true; // link-local
  if (lower.startsWith('fc') || lower.startsWith('fd')) return true; // unique local
  if (lower.startsWith('::ffff:')) return isPrivateIPv4(lower.slice(7));
  return false;
}
