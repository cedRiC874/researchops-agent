// Presentation semantics only; this module does not invoke ResearchOps runtime code.
export const ABSENCE = Object.freeze({
  unknown: '未知', not_provided: '未提供', not_applicable: '不适用', not_published: '未公开',
});
export function known(value, note = '', source = '') {
  return { value, availability: 'known', note, source };
}
export function absent(availability = 'not_provided', note = '', source = '') {
  if (!Object.hasOwn(ABSENCE, availability)) throw new Error('Invalid absence semantics');
  return { value: null, availability, note, source };
}
export function display(field) {
  if (!field || field.availability === undefined) return ABSENCE.not_provided;
  if (field.availability !== 'known') return Object.hasOwn(ABSENCE, field.availability) ? ABSENCE[field.availability] : ABSENCE.unknown;
  // Null cannot become zero (nor a literal "null" in the UI).
  if (field.value === null || field.value === undefined) return ABSENCE.unknown;
  if (typeof field.value === 'boolean') return field.value ? '是' : '否';
  return String(field.value);
}
export function pageOf(items, query = '', type = 'all', page = 1, size = 8) {
  const term = query.trim().toLocaleLowerCase();
  const filtered = items.filter(item => (type === 'all' || item.type === type)
    && `${item.id} ${item.label} ${item.detail || ''}`.toLocaleLowerCase().includes(term));
  const pageSize = Number.isInteger(size) ? Math.min(100, Math.max(1, size)) : 8;
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const current = Math.min(pages, Math.max(1, Number.isInteger(page) ? page : 1));
  return { items: filtered.slice((current - 1) * pageSize, current * pageSize),
    count: filtered.length, total: items.length, page: current, pages };
}
export function safeIdentifier(value) {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value);
}
