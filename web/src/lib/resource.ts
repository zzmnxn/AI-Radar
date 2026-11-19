import { useEffect, useState } from "react";

type Resource<T> = { data: T } | { error: unknown } | null;

export function useResource<T>(read: (signal: AbortSignal) => Promise<T>): Resource<T> {
  const [result, setResult] = useState<{ read: typeof read; value: Resource<T> } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    read(controller.signal).then(
      (data) => { if (!controller.signal.aborted) setResult({ read, value: { data } }); },
      (error) => { if (!controller.signal.aborted) setResult({ read, value: { error } }); },
    );
    return () => controller.abort();
  }, [read]);
  return result?.read === read ? result.value : null;
}
