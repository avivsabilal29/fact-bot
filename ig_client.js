/**
 * KlarifAI - Instagram Private API Client
 * Like Baileys for WhatsApp — direct login, read comments, reply.
 * 
 * This script runs alongside the Python FastAPI server.
 * It polls for @mentions and triggers replies.
 */

const { IgApiClient } = require('instagram-private-api');
const fs = require('fs');
const path = require('path');

const SESSION_FILE = path.join(__dirname, 'data', 'ig_session.json');
const CHECKED_FILE = path.join(__dirname, 'data', 'ig_checked.txt');
const IG_USERNAME = process.env.IG_USERNAME || 'factacheckfact';
const IG_PASSWORD = process.env.IG_PASSWORD || '';
const POLL_INTERVAL = parseInt(process.env.IG_POLL_INTERVAL || '15') * 1000; // 15 detik

// Colors for console
const C = {
  reset: '\x1b[0m',
  green: '\x1b[32m',
  cyan: '\x1b[36m',
  yellow: '\x1b[33m',
  red: '\x1b[31m',
  blue: '\x1b[34m',
};

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function log(level, msg) {
  const ts = new Date().toISOString().slice(11, 19);
  const colors = { info: C.cyan, warn: C.yellow, error: C.red, success: C.green };
  console.log(`${C.blue}[${ts}]${C.reset} ${colors[level] || ''}[${level.toUpperCase()}]${C.reset} ${msg}`);
}

function loadChecked() {
  try {
    return new Set(fs.readFileSync(CHECKED_FILE, 'utf-8').split('\n').filter(Boolean));
  } catch { return new Set(); }
}

function saveChecked(id) {
  fs.appendFileSync(CHECKED_FILE, id + '\n');
}

function loadSession() {
  try { return JSON.parse(fs.readFileSync(SESSION_FILE, 'utf-8')); } 
  catch { return null; }
}

function saveSession(ig) {
  fs.mkdirSync(path.dirname(SESSION_FILE), { recursive: true });
  // Save cookies and basic device info
  const state = {
    cookies: ig.state.cookieStore,
    deviceString: ig.state.deviceString,
    deviceId: ig.state.deviceId,
    uuid: ig.state.uuid,
    phoneId: ig.state.phoneId,
    adid: ig.state.adid,
  };
  fs.writeFileSync(SESSION_FILE, JSON.stringify(state, null, 2));
  log('success', 'Session saved');
}

async function login(ig) {
  // Try to restore session first
  const saved = loadSession();
  if (saved) {
    try {
      ig.state.deviceString = saved.deviceString;
      ig.state.deviceId = saved.deviceId;
      ig.state.uuid = saved.uuid;
      ig.state.phoneId = saved.phoneId;
      ig.state.adid = saved.adid;
      if (saved.cookies) {
        ig.state.cookieStore = saved.cookies;
      }
      const user = await ig.account.currentUser();
      log('success', `Session restored! Logged in as @${user.username}`);
      return user;
    } catch (e) {
      log('warn', `Session expired: ${e.message}. Re-login...`);
    }
  }

  // Fresh login
  ig.state.generateDevice(IG_USERNAME);
  // Skip QE sync for compatibility
  ig.state.qeSyncEnabled = false;
  const user = await ig.account.login(IG_USERNAME, IG_PASSWORD);
  log('success', `Logged in as @${user.username}`);

  // Save session
  saveSession(ig);
  
  return user;
}

async function checkMentions(ig) {
  try {
    const checked = loadChecked();
    
    // Try news feed first
    let notifications = [];
    try {
      const newsFeed = ig.feed.news();
      notifications = await newsFeed.items();
    } catch {
      // Fallback: check timeline posts for new comments
      log('info', 'News feed unavailable, checking timeline...');
      const timelineFeed = ig.feed.timeline();
      const timelineItems = await timelineFeed.items();
      
      for (const item of timelineItems.slice(0, 5)) {
        try {
          const mediaId = item.id || item.pk;
          const commentsFeed = ig.feed.mediaComments(mediaId);
          const comments = await commentsFeed.items();
          
          for (const comment of comments) {
            notifications.push({
              pk: comment.pk,
              text: comment.text,
              user: comment.user,
              media_id: mediaId,
              comment_id: comment.pk,
              type: 'comment',
            });
          }
        } catch {}
      }
    }
    let newCount = 0;

    for (const notif of notifications) {
      const id = notif.pk?.toString() || notif.id?.toString();
      const text = notif.text || notif.title || '';
      const username = notif.user?.username || 'unknown';

      if (!id || checked.has(id)) continue;

      // Check if it's a comment mention
      if (notif.type === 2 || notif.type === 'comment') {
        log('info', `📬 Notification: ${text.slice(0, 100)}`);
        
        if (text.toLowerCase().includes(`@${IG_USERNAME}`.toLowerCase())) {
          log('success', `🎯 Mention detected from @${username}!`);
          
          // Try to get the media and reply
          try {
            const mediaId = notif.media_id || notif.target?.media_id;
            if (mediaId) {
              const media = await ig.media.info(mediaId);
              const caption = media.items[0]?.caption?.text || '';
              
              log('info', `📷 Media caption: "${caption.slice(0, 100)}"`);
              log('info', `🔍 Claim: "${text.replace(`@${IG_USERNAME}`, '').trim().slice(0, 100)}"`);

              // Reply to the comment
              const commentId = notif.comment_id || notif.target?.comment_id;
              if (commentId) {
                const reply = `🤖 Hai @${username}! KlarifAI menerima klaim Anda. Verifikasi sedang diproses... 🔍`;
                await ig.media.comment({
                  mediaId: mediaId,
                  text: reply,
                  replyToCommentId: commentId,
                });
                log('success', `✅ Replied to @${username}`);
                newCount++;
              }
            }
          } catch (e) {
            log('error', `Failed to process/reply: ${e.message}`);
          }
        }
      }
      saveChecked(id);
    }
    
    return newCount;
  } catch (e) {
    log('error', `Poll error: ${e.message}`);
    return 0;
  }
}

async function main() {
  console.log(`
  ╔══════════════════════════════════╗
  ║   🤖 KlarifAI IG Private API    ║
  ║   Like Baileys for Instagram     ║
  ╚══════════════════════════════════╝
  `);

  if (!IG_PASSWORD) {
    log('error', 'IG_PASSWORD not set! Set environment variable or edit this script.');
    log('info', `Login as: @${IG_USERNAME}`);
    process.exit(1);
  }

  const ig = new IgApiClient();
  
  // Handle rate limits / errors gracefully
  ig.request.end$.subscribe(async () => {
    await sleep(1000); // Delay between requests
  });

  try {
    const user = await login(ig);
    log('info', `Polling for @mentions every ${POLL_INTERVAL/1000}s...`);
    
    // Simulate post-login flow
    ig.simulate.postLoginFlow().catch(() => {});
    
    while (true) {
      const count = await checkMentions(ig);
      if (count > 0) {
        log('success', `📬 Replied to ${count} mention(s)`);
      }
      await sleep(POLL_INTERVAL);
    }
  } catch (e) {
    log('error', `Fatal: ${e.message}`);
    process.exit(1);
  }
}

// Handle graceful shutdown
process.on('SIGINT', () => {
  log('info', 'Shutting down...');
  process.exit(0);
});

// Run
main();
