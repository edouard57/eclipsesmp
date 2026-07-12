const { DistributionAPI } = require('helios-core/common')

const ConfigManager = require('./configmanager')

// TODO: once this repo is pushed to GitHub, replace with:
// https://raw.githubusercontent.com/edouard57/eclipsesmp/main/distribution.json
exports.REMOTE_DISTRO_URL = 'https://raw.githubusercontent.com/edouard57/eclipsesmp/main/distribution.json'

const api = new DistributionAPI(
    ConfigManager.getLauncherDirectory(),
    null, // Injected forcefully by the preloader.
    null, // Injected forcefully by the preloader.
    exports.REMOTE_DISTRO_URL,
    false
)

exports.DistroAPI = api