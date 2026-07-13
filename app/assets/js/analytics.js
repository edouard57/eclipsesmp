const got = require('got')

const { LoggerUtil } = require('helios-core')

const logger = LoggerUtil.getLogger('Analytics')

// Not a real secret (this repo, and therefore the built app, is public --
// anyone can extract it from app.asar). It only deters casual/opportunistic
// abuse of the public endpoint, matched server-side against the same value.
const TRACK_URL = 'https://eclipsesmp.cubi-mc.fr/api/track'
const SHARED_SECRET = '1a1c486099feae6750a2a4a1e163d3195192d19d8a618156'

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
