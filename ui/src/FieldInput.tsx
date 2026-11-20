import type { Field } from "./api";

export type FieldValue = string | boolean | File[];

function label(field: Field): string {
  return field.positional ? (field.metavar ?? field.dest).replace(/_/g, " ") : field.flags.join(", ");
}

// One generated input per argument; the kind comes from the command's own parser.
export function FieldInput({ field, value, onChange }: {
  field: Field;
  value: FieldValue | undefined;
  onChange: (value: FieldValue) => void;
}) {
  const id = `field-${field.dest}`;
  const text = typeof value === "string" ? value : "";
  const heading = (
    <span>
      {label(field)}{field.required && <abbr title="required"> *</abbr>}
    </span>
  );
  const help = field.help && <small className="hint">{field.help}</small>;

  switch (field.kind) {
    case "flag":
      return (
        <label className="check" htmlFor={id}>
          <input id={id} type="checkbox" checked={value === true}
                 onChange={(e) => onChange(e.target.checked)} />
          {heading} {help}
        </label>
      );
    case "choice":
      return (
        <label htmlFor={id}>
          {heading}
          <select id={id} value={text || String(field.default ?? "")}
                  onChange={(e) => onChange(e.target.value)}>
            {!field.required && field.default == null && <option value="">—</option>}
            {(field.choices ?? []).map((c) => <option key={String(c)} value={String(c)}>{c}</option>)}
          </select>
          {help}
        </label>
      );
    case "input-file":
    case "input-dir":
      return (
        <label htmlFor={id}>
          {heading}
          <input id={id} type="file" multiple={field.kind === "input-dir"}
                 onChange={(e) => onChange(Array.from(e.target.files ?? []))} />
          {help}
        </label>
      );
    default: {
      const multiline = field.multiple;
      const placeholder = field.default != null && field.default !== false
        ? String(field.default) : (field.kind.startsWith("output") ? "file name in the Run" : "");
      return (
        <label htmlFor={id}>
          {heading}{multiline && <small className="hint"> one per line</small>}
          {multiline
            ? <textarea id={id} rows={3} value={text} onChange={(e) => onChange(e.target.value)} />
            : <input id={id} type={field.kind === "number" || field.kind === "count" ? "number" : "text"}
                     min={field.kind === "count" ? 0 : undefined} value={text}
                     placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />}
          {help}
        </label>
      );
    }
  }
}
