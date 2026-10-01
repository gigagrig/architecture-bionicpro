import React, { useEffect, useState } from 'react';
import { api } from '../api';

type Session = { username: string; csrf: string; roles: string[]; provider: string | null;
  consent_required: boolean; profile_saved: boolean };
type ReportRow = { day: string; prosthesis_id: string; model: string; samples: number;
  movements: number; errors: number; avg_response_ms: number | null;
  max_response_ms: number | null; min_battery_pct: number | null };
type Report = { from: string; to: string; timezone: string; rows: ReportRow[] };
const today = new Date().toISOString().slice(0, 10);
const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);

const ReportPage: React.FC = () => {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [yandex, setYandex] = useState(false);
  const [declined, setDeclined] = useState(false);
  const [message, setMessage] = useState('');
  const [from, setFrom] = useState(yesterday);
  const [to, setTo] = useState(today);
  const [report, setReport] = useState<Report | null>(null);
  const load = async () => {
    const response = await api('/auth/session');
    if (response.ok) setSession(await response.json());
    else if (response.status === 401) setSession(null);
    else throw new Error('Не удалось загрузить сессию');
  };
  useEffect(() => {
    (async () => {
      try {
        const config = await api('/auth/config');
        setYandex((await config.json()).yandex_enabled);
        await load();
        if (new URLSearchParams(window.location.search).has('login')) {
          setMessage('Вход не завершён. Попробуйте ещё раз.');
          window.history.replaceState({}, '', '/');
        }
      } catch { setMessage('Сервис недоступен. Обновите страницу.'); }
      finally { setReady(true); }
    })();
  }, []);
  const action = async (path: string, method: string) => {
    setBusy(true); setMessage('');
    try {
      const response = await api(path, { method, headers: { 'X-CSRF-Token': session?.csrf || '' } });
      const body = await response.json();
      if (response.status === 401) { setSession(null); throw new Error('Сессия истекла. Войдите снова.'); }
      if (!response.ok) throw new Error(body.detail || 'Не удалось выполнить запрос');
      if (path === '/auth/logout') {
        setSession(null);
        setReport(null);
        if (body.sso_logout_pending) setMessage('Вы вышли из приложения. Завершить сессию в службе входа не удалось.');
      } else {
        await load();
        setMessage(path === '/api/protected' ? 'Доступ подтверждён.' : 'Настройки профиля сохранены.');
      }
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Ошибка запроса'); }
    finally { setBusy(false); }
  };
  const getReport = async () => {
    setBusy(true); setMessage(''); setReport(null);
    try {
      const response = await api('/api/reports?' + new URLSearchParams({ from, to }));
      const body = await response.json();
      if (response.status === 401) { setSession(null); throw new Error('Сессия истекла. Войдите снова.'); }
      if (!response.ok) throw new Error((body.detail || 'Не удалось получить отчёт') +
        (body.missing_days ? ': ' + body.missing_days.join(', ') : ''));
      const file = await api(body.download_url);
      if (file.status === 401) { setSession(null); throw new Error('Сессия истекла. Войдите снова.'); }
      if (!file.ok) throw new Error('Не удалось загрузить отчёт. Запросите его снова.');
      const contents = await file.json();
      setReport(contents);
      if (!contents.rows.length) setMessage('За этот период у вас нет зарегистрированных протезов.');
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Ошибка запроса'); }
    finally { setBusy(false); }
  };
  const download = async () => {
    if (!report) return;
    setBusy(true); setMessage('');
    try {
      // Obtain a fresh short-lived link, then download the actual CDN response.
      const linkResponse = await api('/api/reports?' + new URLSearchParams({ from: report.from, to: report.to }));
      if (linkResponse.status === 401) { setSession(null); throw new Error('Сессия истекла. Войдите снова.'); }
      if (!linkResponse.ok) throw new Error('Не удалось подготовить скачивание. Запросите отчёт снова.');
      const response = await api((await linkResponse.json()).download_url);
      if (response.status === 401) { setSession(null); throw new Error('Сессия истекла. Войдите снова.'); }
      if (!response.ok) throw new Error('Не удалось скачать отчёт. Попробуйте снова.');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url; link.download = `bionicpro-report-${report.from}-${report.to}.json`;
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Ошибка скачивания'); }
    finally { setBusy(false); }
  };
  const metric = (value: number | null) => value === null ? '—' : value.toFixed(1);
  const button = 'px-4 py-2 bg-blue-700 text-white rounded disabled:opacity-50';
  return <main className="min-h-screen bg-gray-100 flex items-center justify-center p-6">
    <section className="bg-white rounded-xl shadow p-8 max-w-4xl w-full space-y-5">
      <h1 className="text-2xl font-bold">BionicPRO</h1>
      {!ready ? <p>Загрузка…</p> : !session ? <>
        <p>Войдите в личный кабинет. Для входа потребуется одноразовый код из приложения-аутентификатора.</p>
        <div className="flex gap-3 flex-wrap">
          <a className={button} href="/auth/login">Войти</a>
          {yandex && <a className={button} href="/auth/login?provider=yandex">Войти через Яндекс ID</a>}
        </div>
      </> : <>
        <p>Вы вошли как <strong>{session.username}</strong>.</p>
        {session.consent_required && !declined && <div className="border rounded p-4 space-y-3">
          <h2 className="font-bold">Сохранить данные Яндекс ID?</h2>
          <p>Разрешить BionicPRO запросить и сохранить идентификатор, логин, имя, фамилию и email из Яндекса
            для вашего профиля? Данные сохраняются в региональной базе. Отказ не ограничивает вход.
            Удалить сохранённый профиль можно здесь в любое время.</p>
          <button className={button} disabled={busy} onClick={() => action('/auth/consent', 'POST')}>Разрешить</button>{' '}
          <button className="underline" disabled={busy} onClick={() => setDeclined(true)}>Не сейчас</button>
        </div>}
        {session.profile_saved && <button className="underline" disabled={busy}
          onClick={() => action('/auth/profile', 'DELETE')}>Удалить сохранённый профиль Яндекса</button>}
        <div className="border rounded p-4 space-y-3">
          <h2 className="font-bold">Отчёт о работе ваших протезов</h2>
          <p>Выберите до 31 завершённого дня. Даты и показатели приведены по UTC.
            Последняя дата не включается в отчёт.</p>
          <div className="flex gap-3 flex-wrap items-end">
            <label>С <input className="border rounded p-2 block" type="date" value={from}
              disabled={busy} onChange={e => { setFrom(e.target.value); setReport(null); }} /></label>
            <label>До (не включая) <input className="border rounded p-2 block" type="date" value={to}
              disabled={busy} onChange={e => { setTo(e.target.value); setReport(null); }} /></label>
            <button className={button} disabled={busy || !from || !to || from >= to}
              onClick={getReport}>{busy ? 'Загрузка…' : 'Получить отчёт'}</button>
          </div>
          {report && <>
            <p>Период: {report.from} — {report.to} (последний день не включён).</p>
            {!!report.rows.length && <div className="overflow-x-auto">
              <table className="text-sm w-full text-left">
                <caption className="text-left">Показатели по дням и протезам</caption>
                <thead><tr>{['День', 'Протез', 'Измерения', 'Движения', 'Ошибки', 'Средний отклик, мс',
                  'Макс. отклик, мс', 'Мин. заряд, %'].map(title => <th className="p-2" key={title}>{title}</th>)}</tr></thead>
                <tbody>{report.rows.map(row => <tr key={row.day + row.prosthesis_id} className="border-t">
                  <td className="p-2">{row.day}</td><td className="p-2">{row.prosthesis_id}<br />{row.model}</td>
                  <td className="p-2">{row.samples}</td><td className="p-2">{row.movements}</td>
                  <td className="p-2">{row.errors}</td><td className="p-2">{metric(row.avg_response_ms)}</td>
                  <td className="p-2">{metric(row.max_response_ms)}</td><td className="p-2">{metric(row.min_battery_pct)}</td>
                </tr>)}</tbody>
              </table>
            </div>}
            <p>Отсутствие измерений обозначается нулём; отклик и заряд в этом случае не определены.</p>
            <button className={button} disabled={busy} onClick={download}>Скачать отчёт JSON</button>
          </>}
        </div>
        <div className="flex gap-3 flex-wrap">
          <button className={button} disabled={busy} onClick={() => action('/api/protected', 'GET')}>Проверить доступ</button>
          <button className="underline" disabled={busy} onClick={() => action('/auth/logout', 'POST')}>Выйти</button>
        </div>
      </>}
      {message && <p role="status" className="p-3 bg-gray-100 rounded">{message}</p>}
    </section>
  </main>;
};
export default ReportPage;
