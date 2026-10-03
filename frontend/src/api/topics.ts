/**
 * 主题域取数层(问数范围收敛:域清单)。
 *
 * 主题域是语义模型里声明的可答范围:声明 datasets ∩ 模型现有数据集 = 生效
 * scope;提问时带 `topic` 即把范围收敛到该域。两个刻意的口径:
 *
 *   · **过期域不隐藏** —— status="empty_scope"(声明数据集已全部不在模型里)
 *     的域照常返回,由 UI 置灰「已失效」。让它从清单里消失,用户会带着一个
 *     选不中的旧值继续提问;
 *   · **404 不是错误** —— 该数据源没有语义模型 = 没有域可选,调用方按
 *     「空清单」处理(选择器隐藏),而不是弹错误。
 *
 * 域的名字/描述/示例一律是 KB 原文,展示层照显不译。
 */

import { apiGet } from './http'

/** 域的生效状态闭集(后端 topics[].status)。 */
export const TOPIC_STATUSES = ['ok', 'empty_scope'] as const

export interface TopicInfo {
  name: string
  description: string
  synonyms: string[]
  /** 声明的数据集(原始声明,可能含悬空名)。 */
  datasets: string[]
  /** 生效作用域(声明 ∩ 模型现有数据集);empty_scope 时为空。 */
  scope: string[]
  /** ok | empty_scope。 */
  status: string
  metrics: string[]
  /** 示例问句:选择器渲染「从这里开始问」的起始提问。 */
  examples: string[]
}

export interface TopicList {
  datasource: string
  topics: TopicInfo[]
}

export async function fetchTopics(datasource: string): Promise<TopicList> {
  return apiGet<TopicList>(
    `/v1/semantic/topics?datasource=${encodeURIComponent(datasource)}`,
  )
}
