// charts.js — themed ECharts wrappers

// fmt.htmlSafe escapes user-controlled strings (workspace/directory names,
// model ids) before they land in tooltip HTML. app.js imports charts.js too,
// but this binding is only read at chart-render time — long after app.js has
// finished evaluating — so the circular import is safe.
import { fmt } from '/web/app.js';

// All live instances + their ResizeObservers are tracked so render() can
// fully tear them down before clearing the DOM. Without disconnect(), ROs
// stay registered with the browser, holding closures to disposed charts —
// they accumulate across every render and weigh down layout cycles.
const _live = new Map(); // chart instance → ResizeObserver

export function disposeMountedCharts() {
  for (const [c, ro] of _live) {
    try { ro.disconnect(); } catch {}
    if (!c.isDisposed()) c.dispose();
  }
  _live.clear();
}

const PALETTE = ['#4A9EFF', '#7C5CFF', '#3FB68B', '#E8A23B', '#E5484D', '#5BCEDA', '#F472B6'];

const BASE = {
  textStyle: { color: '#E6EDF3', fontFamily: 'Inter' },
  color: PALETTE,
  grid: { left: 36, right: 12, top: 24, bottom: 24, containLabel: true },
};

const X_AXIS = {
  axisLine:  { lineStyle: { color: '#1F2630' } },
  axisLabel: { color: '#8B98A6' },
  axisTick:  { show: false },
};

const Y_AXIS = {
  axisLine:  { show: false },
  axisTick:  { show: false },
  splitLine: { lineStyle: { color: '#1F2630' } },
  axisLabel: { color: '#8B98A6' },
};

const TOOLTIP = {
  trigger: 'axis',
  backgroundColor: '#0F1419',
  borderColor: '#283040',
  borderWidth: 1,
  textStyle: { color: '#E6EDF3', fontFamily: 'Inter', fontSize: 12 },
  padding: [8, 12],
};

function mount(el) {
  const existing = echarts.getInstanceByDom(el);
  if (existing) {
    const oldRo = _live.get(existing);
    if (oldRo) { try { oldRo.disconnect(); } catch {} }
    _live.delete(existing);
    existing.dispose();
  }
  // Canvas renderer is 3-5× faster than SVG for line+area charts and animates
  // far cheaper. Animations disabled — entrance animations on every refresh
  // were dropping frames for 1-2s each render.
  const c = echarts.init(el, null, { renderer: 'canvas' });
  const ro = new ResizeObserver(() => { if (!c.isDisposed()) c.resize(); });
  ro.observe(el);
  _live.set(c, ro);
  return c;
}

const NO_ANIM = { animation: false };

export function lineChart(el, { x, series }) {
  const c = mount(el);
  c.setOption({
    ...NO_ANIM,
    ...BASE,
    tooltip: TOOLTIP,
    legend: { textStyle: { color: '#8B98A6' }, top: 0, right: 0, icon: 'roundRect', itemWidth: 8, itemHeight: 8 },
    xAxis: { ...X_AXIS, type: 'category', data: x, boundaryGap: false },
    yAxis: { ...Y_AXIS, type: 'value' },
    series: series.map(s => ({
      ...s, type: 'line', smooth: true, showSymbol: false,
      areaStyle: { opacity: 0.12 }, lineStyle: { width: 2 },
    })),
  });
  return c;
}

export function barChart(el, { categories, values, color }) {
  const c = mount(el);
  c.setOption({
    ...NO_ANIM,
    ...BASE,
    tooltip: { ...TOOLTIP, axisPointer: { type: 'shadow' } },
    xAxis: { ...X_AXIS, type: 'category', data: categories, axisLabel: { ...X_AXIS.axisLabel, interval: 0, rotate: categories.length > 5 ? 25 : 0 } },
    yAxis: { ...Y_AXIS, type: 'value' },
    series: [{
      type: 'bar', data: values,
      itemStyle: { color: color || PALETTE[0], borderRadius: [4, 4, 0, 0] },
      barMaxWidth: 32,
    }],
  });
  return c;
}

export function stackedBarChart(el, { categories, series, formatter }) {
  const c = mount(el);
  c.setOption({
    ...NO_ANIM,
    ...BASE,
    tooltip: {
      ...TOOLTIP,
      axisPointer: { type: 'shadow' },
      valueFormatter: formatter || (v => Number(v).toLocaleString()),
    },
    legend: {
      textStyle: { color: '#8B98A6' },
      top: 0, right: 0, icon: 'roundRect',
      itemWidth: 8, itemHeight: 8,
    },
    xAxis: {
      ...X_AXIS, type: 'category', data: categories,
      axisLabel: { ...X_AXIS.axisLabel, interval: categories.length > 20 ? 'auto' : 0, rotate: categories.length > 12 ? 45 : 0 },
    },
    yAxis: { ...Y_AXIS, type: 'value' },
    series: series.map((s, i) => ({
      name: s.name,
      type: 'bar',
      stack: 'total',
      data: s.values,
      itemStyle: { color: s.color || PALETTE[i % PALETTE.length] },
      barMaxWidth: 24,
      emphasis: { focus: 'series' },
    })),
  });
  return c;
}

export function groupedBarChart(el, { categories, series, formatter }) {
  const c = mount(el);
  c.setOption({
    ...NO_ANIM,
    ...BASE,
    tooltip: {
      ...TOOLTIP,
      axisPointer: { type: 'shadow' },
      valueFormatter: formatter || (v => Number(v).toLocaleString()),
    },
    legend: {
      textStyle: { color: '#8B98A6' },
      top: 0, right: 0, icon: 'roundRect',
      itemWidth: 8, itemHeight: 8,
    },
    xAxis: {
      ...X_AXIS, type: 'category', data: categories,
      axisLabel: { ...X_AXIS.axisLabel, interval: 0, rotate: categories.length > 5 ? 25 : 0 },
    },
    yAxis: { ...Y_AXIS, type: 'value' },
    series: series.map((s, i) => ({
      name: s.name,
      type: 'bar',
      data: s.values,
      itemStyle: { color: s.color || PALETTE[i % PALETTE.length], borderRadius: [4, 4, 0, 0] },
      barMaxWidth: 24,
      emphasis: { focus: 'series' },
    })),
  });
  return c;
}

export function sankeyChart(el, { nodes, links, formatter }) {
  const c = mount(el);
  // Accept nodes as either ['name', ...] or [{name: 'x'}, ...] — the bipartite
  // workspaces matrix returns objects so it can carry layout hints later.
  const nodeData = nodes.map(n => typeof n === 'string' ? { name: n } : n);
  c.setOption({
    ...NO_ANIM,
    ...BASE,
    tooltip: {
      trigger: 'item',
      backgroundColor: '#0F1419',
      borderColor: '#283040',
      borderWidth: 1,
      textStyle: { color: '#E6EDF3', fontFamily: 'Inter', fontSize: 12 },
      padding: [8, 12],
      formatter: p => {
        const v = formatter ? formatter(p.value) : Number(p.value).toLocaleString();
        if (p.dataType === 'edge') {
          return `${fmt.htmlSafe(p.data.source)} → ${fmt.htmlSafe(p.data.target)}<br/><b>${v}</b>`;
        }
        return `<b>${fmt.htmlSafe(p.name)}</b><br/>${v}`;
      },
    },
    series: [{
      type: 'sankey',
      data: nodeData,
      links,
      emphasis: { focus: 'adjacency' },
      lineStyle: { color: 'gradient', curveness: 0.5, opacity: 0.4 },
      label: { color: '#E6EDF3', fontFamily: 'Inter', fontSize: 11 },
      nodeAlign: 'left',
      left: 8, right: 120, top: 12, bottom: 12,
      itemStyle: { borderColor: '#0F1419', borderWidth: 1 },
    }],
  });
  return c;
}


export function donutChart(el, data) {
  const c = mount(el);
  c.setOption({
    ...NO_ANIM,
    color: PALETTE,
    tooltip: {
      trigger: 'item',
      backgroundColor: '#0F1419', borderColor: '#283040', borderWidth: 1,
      textStyle: { color: '#E6EDF3', fontFamily: 'Inter' },
      formatter: p => `${fmt.htmlSafe(p.name)}<br/><b>${Number(p.value).toLocaleString()}</b> tokens (${p.percent.toFixed(1)}%)`,
    },
    legend: {
      textStyle: { color: '#8B98A6' },
      bottom: 10, icon: 'roundRect', itemWidth: 8, itemHeight: 8,
      type: 'scroll',
    },
    series: [{
      type: 'pie',
      center: ['50%', '44%'],
      radius: ['48%', '68%'],
      avoidLabelOverlap: true,
      padAngle: 2,
      itemStyle: { borderColor: '#0F1419', borderWidth: 2, borderRadius: 4 },
      label: {
        show: true,
        position: 'inside',
        color: '#fff',
        fontSize: 12,
        fontWeight: 600,
        formatter: ({ percent }) => percent >= 6 ? percent.toFixed(0) + '%' : '',
      },
      labelLine: { show: false },
      data,
    }],
  });
  return c;
}
