/**
 * MiniCharts - A tiny SVG-based charting library for HookWise
 */
window.MiniChart = {
    renderBar: function(canvasId, labels, data, color = '#3b82f6') {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const width = canvas.width;
        const height = canvas.height;
        const padding = 30;
        const chartWidth = width - padding * 2;
        const chartHeight = height - padding * 2;
        
        ctx.clearRect(0, 0, width, height);
        
        const max = Math.max(...data, 1);
        const barWidth = chartWidth / data.length * 0.8;
        const spacing = chartWidth / data.length * 0.2;
        
        data.forEach((val, i) => {
            const h = (val / max) * chartHeight;
            const x = padding + i * (barWidth + spacing);
            const y = height - padding - h;
            
            ctx.fillStyle = color;
            ctx.fillRect(x, y, barWidth, h);
            
            ctx.fillStyle = '#94a3b8';
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(labels[i], x + barWidth / 2, height - 10);
            ctx.fillText(val, x + barWidth / 2, y - 5);
        });
    }
};
