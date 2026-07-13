const fs = require('fs')
const path = require('path')
const got = require('got')
const FormData = require('form-data')

const { LoggerUtil } = require('helios-core')

const logger = LoggerUtil.getLogger('Analytics')

// Not a real secret (this repo, and therefore the built app, is public --
// anyone can extract it from app.asar). It only deters casual/opportunistic
// abuse of the public endpoint, matched server-side against the same value.
const TRACK_URL = 'https://eclipsesmp.cubi-mc.fr/api/track'
const CRASH_URL = 'https://eclipsesmp.cubi-mc.fr/api/crash'
const SCREENSHOT_URL = 'https://eclipsesmp.cubi-mc.fr/api/screenshot'
const SHARED_SECRET = '1a1c486099feae6750a2a4a1e163d3195192d19d8a618156'

// Discord's own attachment cap for bot-uploaded files (non-boosted server).
const MAX_CRASH_REPORT_SIZE = 8 * 1024 * 1024

/**
 * Fire-and-forget notification that a player launched the game. Never
 * throws and never delays the launch -- failures are logged and ignored.
 *
 * @param {Object} authUser The selected account (ConfigManager.getSelectedAccount()).
 * @param {string} launcherVersion app.getVersion().
 */
exports.trackLaunch = function(authUser, launcherVersion){
    got.post(TRACK_URL, {
        headers: { 'X-Analytics-Secret': SHARED_SECRET },
        json: {
            username: authUser.displayName,
            type: authUser.type,
            launcherVersion
        },
        timeout: { request: 5000 },
        retry: { limit: 0 }
    }).catch(err => {
        logger.warn('Failed to send launch analytics.', err.message)
    })
}

/**
 * Fire-and-forget notification that the launcher app itself was opened
 * (distinct from actually launching the game -- see kind: 'app_open').
 * No-op if no account is selected yet (e.g. the very first ever launch,
 * before any account has been added) since there's nothing valid to
 * attribute the event to.
 *
 * @param {Object} authUser The selected account (ConfigManager.getSelectedAccount()), may be null.
 * @param {string} launcherVersion app.getVersion().
 */
exports.trackLauncherOpen = function(authUser, launcherVersion){
    if(authUser == null){
        return
    }
    got.post(TRACK_URL, {
        headers: { 'X-Analytics-Secret': SHARED_SECRET },
        json: {
            username: authUser.displayName,
            type: authUser.type,
            launcherVersion,
            kind: 'app_open'
        },
        timeout: { request: 5000 },
        retry: { limit: 0 }
    }).catch(err => {
        logger.warn('Failed to send launcher-open analytics.', err.message)
    })
}

/**
 * Find the most recently modified crash report in gameDir/crash-reports,
 * if any exist. Returns null if the folder is missing or empty.
 *
 * @param {string} gameDir The instance's game directory (ProcessBuilder#gameDir).
 */
function findLatestCrashReport(gameDir){
    const crashDir = path.join(gameDir, 'crash-reports')
    if(!fs.existsSync(crashDir)){
        return null
    }
    const files = fs.readdirSync(crashDir)
        .filter(f => f.endsWith('.txt'))
        .map(f => {
            const full = path.join(crashDir, f)
            return { full, name: f, mtime: fs.statSync(full).mtimeMs }
        })
        .sort((a, b) => b.mtime - a.mtime)
    return files.length > 0 ? files[0] : null
}

/**
 * Fire-and-forget: if the game exited abnormally and a crash report was
 * just written, upload it to the staff Discord channel. Only reports crash
 * reports written within the last `maxAgeMs` (default 60s) so we don't
 * re-send an old leftover report from a previous unrelated session.
 *
 * @param {Object} authUser The selected account.
 * @param {string} launcherVersion app.getVersion().
 * @param {string} gameDir ProcessBuilder#gameDir for this instance.
 */
exports.trackCrash = function(authUser, launcherVersion, gameDir, maxAgeMs = 60000){
    let report
    try {
        report = findLatestCrashReport(gameDir)
    } catch(err){
        logger.warn('Failed to look up crash report.', err.message)
        return
    }
    if(report == null || (Date.now() - report.mtime) > maxAgeMs){
        return
    }

    const stat = fs.statSync(report.full)
    if(stat.size > MAX_CRASH_REPORT_SIZE){
        logger.warn('Crash report too large to upload, skipping.', report.full)
        return
    }

    const form = new FormData()
    form.append('username', authUser.displayName)
    form.append('type', authUser.type)
    form.append('launcherVersion', launcherVersion)
    form.append('file', fs.createReadStream(report.full), report.name)

    got.post(CRASH_URL, {
        headers: { 'X-Analytics-Secret': SHARED_SECRET, ...form.getHeaders() },
        body: form,
        timeout: { request: 15000 },
        retry: { limit: 0 }
    }).catch(err => {
        logger.warn('Failed to send crash report.', err.message)
    })
}

// Discord's own attachment cap for bot-uploaded files (non-boosted server).
const MAX_SCREENSHOT_SIZE = 8 * 1024 * 1024

/**
 * Upload a screenshot to the server's Discord media channel. Unlike the
 * fire-and-forget functions above, this is a manual user action (a button
 * click in the Screenshots settings tab) so the caller needs to know
 * whether it actually succeeded.
 *
 * @param {string} filePath Absolute path to the screenshot file.
 * @param {string} username The selected account's display name.
 * @returns {Promise<void>} Resolves on success, rejects with an Error otherwise.
 */
exports.shareScreenshot = async function(filePath, username){
    const stat = fs.statSync(filePath)
    if(stat.size > MAX_SCREENSHOT_SIZE){
        throw new Error('Screenshot too large to share (max 8 Mo).')
    }

    const form = new FormData()
    form.append('username', username)
    form.append('file', fs.createReadStream(filePath), path.basename(filePath))

    await got.post(SCREENSHOT_URL, {
        headers: { 'X-Analytics-Secret': SHARED_SECRET, ...form.getHeaders() },
        body: form,
        timeout: { request: 15000 },
        retry: { limit: 0 }
    })
}
