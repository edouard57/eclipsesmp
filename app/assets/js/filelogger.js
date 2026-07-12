/**
 * FileLogger
 *
 * Mirrors everything that would normally only be visible in the DevTools
 * console (Ctrl+Shift+I) to a persistent log file, so players/admins can
 * send a file instead of copy-pasting the console when something breaks.
 *
 * Captures both the main process's own console output and the renderer's
 * (winston's console transport, ProcessBuilder's [Minecraft] passthrough,
 * uncaught exceptions, etc.) via Electron's console-message event.
 */
const fs = require('fs')
const path = require('path')

const MAX_LOG_FILES = 10
// eslint-disable-next-line no-control-regex
const ANSI_REGEX = /\x1b\[[0-9;]*m/g

let stream = null

function stripAnsi(str) {
    return String(str).replace(ANSI_REGEX, '')
}

function pad(n) {
    return String(n).padStart(2, '0')
}

function timestampForFilename(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}_${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`
}

function pruneOldLogs(logsDir) {
    try {
        const files = fs.readdirSync(logsDir)
            .filter(f => f.startsWith('launcher-') && f.endsWith('.log'))
            .map(f => ({ name: f, path: path.join(logsDir, f), mtime: fs.statSync(path.join(logsDir, f)).mtimeMs }))
            .sort((a, b) => b.mtime - a.mtime)

        for (const file of files.slice(MAX_LOG_FILES - 1)) {
            fs.unlinkSync(file.path)
        }
    } catch (err) {
        // Non-fatal, logging must never crash the launcher.
    }
}

/**
 * Initialize file logging. Must be called once, early, from the main process.
 *
 * @param {string} userDataDir The Electron userData directory.
 * @returns {string} The path of the log file created for this session.
 */
exports.init = function(userDataDir) {
    const logsDir = path.join(userDataDir, 'logs')
    fs.mkdirSync(logsDir, { recursive: true })
    pruneOldLogs(logsDir)

    const logPath = path.join(logsDir, `launcher-${timestampForFilename(new Date())}.log`)
    stream = fs.createWriteStream(logPath, { flags: 'a' })

    // Mirror the main process's own console output.
    for (const method of ['log', 'info', 'warn', 'error', 'debug']) {
        const original = console[method].bind(console)
        console[method] = (...args) => {
            original(...args)
            exports.write(args.map(a => (typeof a === 'string' ? a : JSON.stringify(a))).join(' '))
        }
    }

    process.on('uncaughtException', (err) => {
        exports.write(`[main] Uncaught exception: ${err && err.stack ? err.stack : err}`)
    })

    return logPath
}

/**
 * Append a line to the current session's log file. Safe to call before
 * init() (writes are dropped) or after the stream has closed.
 *
 * @param {string} line The line to append (timestamp is added automatically).
 */
exports.write = function(line) {
    if (stream == null) return
    stream.write(`[${new Date().toISOString()}] ${stripAnsi(line)}\n`)
}

/**
 * Mirror a BrowserWindow's renderer console (DevTools console) to the log
 * file. Covers winston's console transport, ProcessBuilder's stdout/stderr
 * passthrough, and any uncaught renderer-side errors.
 *
 * @param {Electron.WebContents} webContents The window's webContents.
 */
exports.attachRenderer = function(webContents) {
    const LEVEL_NAMES = { 0: 'verbose', 1: 'info', 2: 'warn', 3: 'error' }
    // Electron has shipped two incompatible shapes for this event across
    // versions: (event, level, message, line, sourceId) and
    // (event, { level, message, ... }). Support both defensively.
    webContents.on('console-message', (...args) => {
        const [, second, third] = args
        const isObjectShape = second != null && typeof second === 'object'
        const level = isObjectShape ? second.level : second
        const message = isObjectShape ? second.message : third
        const levelName = LEVEL_NAMES[level] ?? String(level)
        exports.write(`[renderer/${levelName}] ${message}`)
    })
}
