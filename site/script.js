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

// Live server status: real ping, not a decorative mockup.
async function loadServerStatus() {
    const pingBars = document.getElementById('ping-bars')
    const motdEl = document.getElementById('server-motd')
    const slotsEl = document.getElementById('server-slots')
    try {
        const res = await fetch('/status')
        const data = await res.json()
        if (data.online) {
            pingBars.classList.remove('offline')
            pingBars.title = 'En ligne'
            motdEl.textContent = data.motd || 'Une survie entre amis · Fabric 26.2'
            slotsEl.textContent = `${data.players_online}/${data.players_max} en jeu · whitelist`
        } else {
            pingBars.classList.add('offline')
            pingBars.title = 'Hors ligne'
            slotsEl.textContent = 'Hors ligne pour le moment · whitelist'
        }
    } catch (e) {
        pingBars.classList.add('offline')
    }
}
loadServerStatus()
setInterval(loadServerStatus, 30000)

// Scroll reveal: sections and transcript lines fade/slide in once, first
// time they enter view. Respects prefers-reduced-motion (see styles.css --
// the observer still runs but the CSS transition is instant there).
const sectionTargets = [...document.querySelectorAll('section')]
const lineTargets = [...document.querySelectorAll('.transcript .line')]
const revealTargets = [...sectionTargets, ...lineTargets]

sectionTargets.forEach((el) => el.classList.add('reveal'))
lineTargets.forEach((el, i) => {
    el.classList.add('reveal', 'reveal-line')
    el.style.transitionDelay = `${i * 90}ms`
})

const revealObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
        if (entry.isIntersecting) {
            entry.target.classList.add('revealed')
            revealObserver.unobserve(entry.target)
        }
    }
}, { threshold: 0.15 })

revealTargets.forEach((el) => revealObserver.observe(el))
