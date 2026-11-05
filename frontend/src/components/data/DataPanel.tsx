import { useEffect, useState } from 'react'
import { useT } from '@/i18n'
import type { Key } from '@/i18n/catalog'
import { DATA_TABS, href, type DataRoute } from '@/lib/route'
import { AskPage } from './AskPage'
import { ExplorePage } from './ExplorePage'
import { HistoryPage } from './HistoryPage'
import { RunPage } from './RunPage'
import './data.css'

const TAB_LABEL: Record<(typeof DATA_TABS)[number], Key> = {
  ask: 'sq.tab.ask',
  explore: 'sq.tab.explore',
  history: 'sq.tab.history',
}

/**
 * 数据：SQL Agent 的那一半控制台（原来是 SQL Agent 自己挂在 / 上的页面）。
 *
 * 所有请求都走 /sql-api/（api/sql/client.ts），浏览器手里没有 SQL Agent 的 Key。
 *
 * 问数页切走时只隐藏不卸载：一个问题可能要跑几十秒，去数据浏览里查一眼表，回来不能丢；
 * 别的几页每次进来重新取，本来就该是新的。
 */
export function DataPanel({ route }: { route: DataRoute }) {
  const { t } = useT()
  const [askMounted, setAskMounted] = useState(route.page === 'ask')
  useEffect(() => { if (route.page === 'ask') setAskMounted(true) }, [route.page])
  // 一次查询的经过属于查询历史那一栏（从历史、从对话轮次跳过来的都是历史上的查询）
  const activeTab = route.page === 'run' ? 'history' : route.page

  return (
    <div className="admin data">
      <div className="admin-head">
        <p className="admin-lede">{t('sq.lede')}</p>
        <div className="tabs" role="tablist" aria-label={t('sq.tabs')}>
          {DATA_TABS.map((id) => (
            <a key={id} role="tab" aria-selected={activeTab === id}
               href={id === 'ask' ? href.dataAsk() : id === 'explore' ? href.dataExplore() : href.dataHistory()}
               className={`tab${activeTab === id ? ' is-active' : ''}`}>
              {t(TAB_LABEL[id])}
            </a>
          ))}
        </div>
      </div>

      {askMounted && <div className="data-ask-slot" hidden={route.page !== 'ask'}><AskPage active={route.page === 'ask'} /></div>}
      {route.page === 'explore' && (
        <ExplorePage database={route.database ?? ''} table={route.table ?? ''} query={route.query} />
      )}
      {route.page === 'history' && <HistoryPage />}
      {route.page === 'run' && <RunPage runId={route.runId} />}
    </div>
  )
}
