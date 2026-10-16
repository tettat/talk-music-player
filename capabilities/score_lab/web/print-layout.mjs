// Pure helpers shared by the PDF page and the export action.
export function printRequest(sampleId, revisionId = null) {
  if (typeof sampleId !== 'string' || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(sampleId) ||
      revisionId !== null && (typeof revisionId !== 'string' || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(revisionId))) {
    throw new Error('乐谱版本无效');
  }
  return {sampleId, revisionId};
}

export function printURL(base, sampleId, revisionId = null, autoPrint = false) {
  const request = printRequest(sampleId, revisionId);
  const query = new URLSearchParams({sample: request.sampleId});
  if (request.revisionId !== null) query.set('revision', request.revisionId);
  if (autoPrint) query.set('print', '1');
  return `${base}/print.html?${query}`;
}

export function printableABC(original) {
  if (typeof original !== 'string' || !original.trim() || new TextEncoder().encode(original).length > 512 * 1024) {
    throw new Error('此乐谱暂不可导出');
  }
  // The page supplies its own title; discard screen-layout directives only.
  // Musical tokens, voices, chord symbols, rests, ties, key and meter are retained.
  let meter = [4, 4], unit = [1, 8];
  const source = original.replace(/^[TC]:.*(?:\r?\n|$)/gm, '')
    .replace(/^%%(?:topspace|titlespace|musicspace|sysstaffsep|staffwidth|pagewidth|pageheight|scale)\b.*$/gm, '')
    .replaceAll('name="Vocal Melody"', 'name="人声旋律"')
    .replaceAll('name="Ins Melody"', 'name="器乐旋律"');
  const expanded = source.split(/\r?\n/).map(line => {
    const m = line.match(/^M:\s*(\d+)\/(\d+)/); if (m) meter = m.slice(1).map(Number);
    const l = line.match(/^L:\s*(\d+)\/(\d+)/); if (l) unit = l.slice(1).map(Number);
    if (/^[A-Za-z]:|^%/.test(line)) return line;
    // Equivalent complete-bar rests keep otherwise silent voices on each system.
    return line.replace(/"[^"\n]*"|\[[^\]\n]*\]|Z(\d*)\|/g, (token, count) => {
      if (count === undefined) return token;
      const bars = Number(count || 1), units = meter[0] * unit[1] / (meter[1] * unit[0]);
      if (!Number.isSafeInteger(bars) || bars < 1 || bars > 1500 || !Number.isFinite(units) || units <= 0) throw new Error('休止小节无效');
      return (`z${units}|`).repeat(bars);
    });
  }).join('\n');
  return expanded.replace(/^X:.*$/m, '$&\n%%topspace 0\n%%musicspace 5\n%%sysstaffsep 28');
}

export function paginateSystems(heights, headerHeight, pageHeight, gap) {
  if (!heights.length || ![headerHeight, pageHeight, gap, ...heights].every(Number.isFinite) ||
      pageHeight <= 0 || headerHeight < 0 || gap < 0 || heights.some(h => h <= 0 || h + gap > pageHeight) ||
      headerHeight + heights[0] + gap > pageHeight) throw new Error('谱面超出纸张');
  const pages = [[]]; let used = headerHeight;
  heights.forEach((height, index) => {
    if (used + height + gap > pageHeight) {pages.push([]); used = 0;}
    pages.at(-1).push(index); used += height + gap;
  });
  return pages;
}
