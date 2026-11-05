export function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export function milliseconds(value: number): string {
  return value < 10 ? value.toFixed(2) : value < 100 ? value.toFixed(1) : Math.round(value).toString();
}
