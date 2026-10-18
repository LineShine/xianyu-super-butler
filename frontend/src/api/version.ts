/**
 * 版本检测 API
 *
 * 功能：
 * 1. 获取系统当前版本号
 * 2. 向后端请求检查更新（后端再向远程更新服务器代理请求）
 */
import { get } from '@/utils/request'
import type { ApiResponse } from '@/types'

const PREFIX = '/api/v1/version'

/** 当前版本信息 */
export interface CurrentVersion {
  version: string
}

/** 获取当前版本号 */
export const getCurrentVersion = async (): Promise<
  ApiResponse & { data?: CurrentVersion }
> => {
  return get(`${PREFIX}/current`)
}
