// Documentation : surligne dans le sommaire la section en cours de lecture (la dernière
// dont le titre a passé le premier quart de l'écran). Même script dans docs.html du site
// (scripts/sync_site_docs.py). Écoute en capture : dans l'application, c'est .content qui
// défile, pas la fenêtre.
(function () {
    const links = [...document.querySelectorAll('.guide-toc a[href^="#"]')];
    const sections = links.map(a => document.getElementById(a.getAttribute('href').slice(1))).filter(Boolean);
    if (!sections.length) return;
    let pending = false;
    function mark() {
        pending = false;
        const line = window.innerHeight * 0.25;
        let current = sections[0];
        for (const s of sections) { if (s.getBoundingClientRect().top <= line) current = s; }
        const last = sections[sections.length - 1];
        if (last.getBoundingClientRect().bottom <= window.innerHeight + 2) current = last;
        links.forEach(a => a.classList.toggle('active', a.getAttribute('href') === '#' + current.id));
    }
    const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(mark); } };
    document.addEventListener('scroll', schedule, { capture: true, passive: true });
    window.addEventListener('resize', schedule);
    window.addEventListener('hashchange', () => setTimeout(mark, 50));
    mark();
})();
