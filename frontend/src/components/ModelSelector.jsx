import { modelLabel } from '../theme';
import { Skeleton } from './primitives';

/**
 * One filter row above everything it scopes — sections 1, 2 and 4 all
 * re-render against the selected model.
 */
export default function ModelSelector({ models, value, onChange, isLoading }) {
  const active = models.find((entry) => entry.name === value);

  return (
    <div className="field">
      <label className="field-label" htmlFor="model-select">
        Attribution model
      </label>
      {isLoading ? (
        <Skeleton height={38} radius={8} />
      ) : (
        <select
          id="model-select"
          className="select"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          aria-describedby="model-help"
        >
          {models.map((entry) => (
            <option key={entry.name} value={entry.name}>
              {entry.label || modelLabel(entry.name)}
            </option>
          ))}
        </select>
      )}
      <p className="field-help" id="model-help" title={active?.description || ''}>
        {active?.description || ''}
      </p>
    </div>
  );
}
