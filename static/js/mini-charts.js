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
    },
    renderMultiBar: function(canvasId, labels, datasets) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const width = canvas.width;
        const height = canvas.height;
        const padding = 30;
        const chartWidth = width - padding * 2;
        const chartHeight = height - padding * 2;
        
        ctx.clearRect(0, 0, width, height);
        if (!labels || labels.length === 0 || !datasets || datasets.length === 0) return;
        
        let max = 1;
        datasets.forEach(ds => {
            const dsMax = Math.max(...ds.data);
            if (dsMax > max) max = dsMax;
        });

        const numGroups = labels.length;
        const numBarsPerGroup = datasets.length;
        
        const groupWidth = chartWidth / numGroups * 0.8;
        const groupSpacing = chartWidth / numGroups * 0.2;
        const barWidth = groupWidth / numBarsPerGroup;
        
        labels.forEach((label, i) => {
            const groupX = padding + i * (groupWidth + groupSpacing);
            
            datasets.forEach((ds, j) => {
                const val = ds.data[i] || 0;
                const h = (val / max) * chartHeight;
                const x = groupX + j * barWidth;
                const y = height - padding - h;
                
                ctx.fillStyle = ds.color || '#3b82f6';
                ctx.fillRect(x, y, barWidth - 1, h);
                
                if (val > 0) {
                    ctx.fillStyle = '#94a3b8';
                    ctx.font = '9px sans-serif';
                    ctx.textAlign = 'center';
                    ctx.fillText(val, x + barWidth / 2, y - 4);
                }
            });
            
            ctx.fillStyle = '#94a3b8';
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(label, groupX + groupWidth / 2, height - 10);
        });

        let legendX = width - padding;
        datasets.slice().reverse().forEach(ds => {
            ctx.font = '10px sans-serif';
            ctx.textAlign = 'right';
            const textWidth = ctx.measureText(ds.name).width;
            
            ctx.fillStyle = '#94a3b8';
            ctx.fillText(ds.name, legendX, 10);
            
            ctx.fillStyle = ds.color || '#3b82f6';
            ctx.fillRect(legendX - textWidth - 12, 2, 8, 8);
            
            legendX -= textWidth + 20;
        });
    },
    renderLine: function(canvasId, data, color = '#3b82f6') {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        const width = canvas.width;
        const height = canvas.height;
        
        ctx.clearRect(0, 0, width, height);
        if (!data || data.length < 2) return;

        const max = Math.max(...data, 1);
        const step = width / (data.length - 1);
        
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.lineJoin = 'round';
        
        data.forEach((val, i) => {
            const x = i * step;
            const y = height - (val / max) * height;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        
        ctx.stroke();
        
        // Fill area
        ctx.lineTo(width, height);
        ctx.lineTo(0, height);
        ctx.fillStyle = color + '22'; // 13% opacity
        ctx.fill();
    }
};
