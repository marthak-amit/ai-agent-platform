import { useCallback, useEffect, useRef, useState } from "react";
import { problemFrom } from "../../api/admin";

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string;
  reload: () => void;
}

/** Run an async loader on mount and whenever `deps` change; ignores results from superseded calls. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const latest = useRef(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    const id = ++latest.current;
    setLoading(true);
    setError("");
    loaderRef
      .current()
      .then((result) => {
        if (id === latest.current) setData(result);
      })
      .catch((err) => {
        if (id === latest.current) setError(problemFrom(err).message);
      })
      .finally(() => {
        if (id === latest.current) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, loading, error, reload };
}
