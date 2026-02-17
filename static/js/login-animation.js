/**
 * Login Page Background Animation
 * Theme: "Floating Tickets" - Subtle rectangles floating upwards
 */

document.addEventListener('DOMContentLoaded', () => {
    const canvas = document.getElementById('bg-animation');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    let width, height;
    let tickets = [];

    // Configuration
    const TICKET_COUNT = 15;
    const COLORS = [
        'rgba(56, 189, 248, 0.05)',  // Primary (Light Blue)
        'rgba(192, 132, 252, 0.05)', // Accent (Purple)
        'rgba(16, 185, 129, 0.05)',  // Success (Green)
        'rgba(255, 255, 255, 0.03)'  // White (Subtle)
    ];

    class Ticket {
        constructor() {
            this.reset(true);
        }

        reset(initial = false) {
            this.x = Math.random() * width;
            this.y = initial ? Math.random() * height : height + 100;
            this.speed = 0.2 + Math.random() * 0.5;
            this.size = 30 + Math.random() * 50; // Width
            this.height = this.size * 0.6;       // Aspect ratio
            this.color = COLORS[Math.floor(Math.random() * COLORS.length)];
            this.rotation = Math.random() * Math.PI * 2;
            this.rotationSpeed = (Math.random() - 0.5) * 0.002;
            this.wobble = Math.random() * Math.PI * 2;
            this.wobbleSpeed = 0.01 + Math.random() * 0.02;
        }

        update() {
            this.y -= this.speed;
            this.rotation += this.rotationSpeed;
            this.wobble += this.wobbleSpeed;
            this.x += Math.sin(this.wobble) * 0.5;

            if (this.y < -100) {
                this.reset();
            }
        }

        draw() {
            ctx.save();
            ctx.translate(this.x, this.y);
            ctx.rotate(this.rotation);

            // Draw Ticket Body
            ctx.fillStyle = this.color;
            ctx.beginPath();
            ctx.roundRect(-this.size / 2, -this.height / 2, this.size, this.height, 4);
            ctx.fill();

            // Draw "Text Lines" inside
            ctx.fillStyle = 'rgba(255, 255, 255, 0.1)';
            const padding = this.size * 0.1;
            const lineHeight = this.height * 0.15;

            // Header line
            ctx.fillRect(
                -this.size / 2 + padding,
                -this.height / 2 + padding,
                this.size * 0.6,
                lineHeight
            );

            // Body line 1
            ctx.fillRect(
                -this.size / 2 + padding,
                -this.height / 2 + padding + lineHeight * 2,
                this.size - padding * 2,
                lineHeight
            );

            // Body line 2
            ctx.fillRect(
                -this.size / 2 + padding,
                -this.height / 2 + padding + lineHeight * 4,
                this.size * 0.8,
                lineHeight
            );

            ctx.restore();
        }
    }

    function resize() {
        width = canvas.width = window.innerWidth;
        height = canvas.height = window.innerHeight;
    }

    function init() {
        resize();
        window.addEventListener('resize', resize);

        for (let i = 0; i < TICKET_COUNT; i++) {
            tickets.push(new Ticket());
        }

        animate();
    }

    function animate() {
        if (!document.hidden) {
            ctx.clearRect(0, 0, width, height);

            tickets.forEach(ticket => {
                ticket.update();
                ticket.draw();
            });
        }

        requestAnimationFrame(animate);
    }

    init();
});
