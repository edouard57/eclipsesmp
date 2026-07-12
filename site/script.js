// Nav shadow on scroll
const nav = document.getElementById('nav')
window.addEventListener('scroll', () => {
    nav.classList.toggle('scrolled', window.scrollY > 8)
}, { passive: true })

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
