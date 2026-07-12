// Nav shadow on scroll
const nav = document.getElementById('nav')
window.addEventListener('scroll', () => {
    nav.classList.toggle('scrolled', window.scrollY > 8)
}, { passive: true })

// Copy server IP
const copyBtn = document.getElementById('copy-btn')
const copyLabel = document.getElementById('copy-label')
const serverIp = document.getElementById('server-ip').textContent.trim()

copyBtn.addEventListener('click', async () => {
    try {
        await navigator.clipboard.writeText(serverIp)
    } catch (err) {
        // Fallback for non-secure or unsupported contexts.
        const ta = document.createElement('textarea')
        ta.value = serverIp
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
    }
    copyLabel.textContent = 'copie !'
    setTimeout(() => { copyLabel.textContent = 'copier' }, 1800)
})

// Highlight the visitor's likely platform.
function detectPlatform() {
    const ua = navigator.userAgent
    if (/Win/.test(ua)) return 'windows'
    if (/Mac/.test(ua)) return 'mac'
    if (/Linux|X11/.test(ua)) return 'linux'
    return null
}

const detected = detectPlatform()
if (detected) {
    const card = document.getElementById(`card-${detected}`)
    if (card) card.classList.add('detected')
}
