#!/usr/bin/env python3
"""
日志存储管理工具
用于查看、查询和导出存储的通知日志
"""

import sys
import os
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# 添加src目录到Python路径
src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src')
sys.path.insert(0, src_path)

from utils.log_storage import LogStorage


def show_statistics(storage: LogStorage):
    """显示存储统计信息"""
    stats = storage.get_statistics()
    
    print("=" * 50)
    print("📊 日志存储统计信息")
    print("=" * 50)
    print(f"总记录数: {stats.get('total_records', 0)}")
    print(f"最近24小时记录: {stats.get('records_last_day', 0)}")
    print(f"最近7天记录: {stats.get('records_last_week', 0)}")
    print(f"存储位置: {stats.get('storage_path', 'Unknown')}")
    print()
    
    print("📈 事件类型分布:")
    event_dist = stats.get('event_distribution', {})
    if event_dist:
        for event_type, count in sorted(event_dist.items(), key=lambda x: x[1], reverse=True):
            print(f"  {event_type}: {count}")
    else:
        print("  暂无数据")
    print("=" * 50)


def show_recent_logs(storage: LogStorage, hours: int = 24):
    """显示最近的日志"""
    logs = storage.get_recent_logs(hours)
    
    print("=" * 80)
    print(f"🕒 最近 {hours} 小时的日志记录 (共 {len(logs)} 条)")
    print("=" * 80)
    
    if not logs:
        print("暂无记录")
        return
    
    for i, log in enumerate(logs, 1):
        print(f"序号: {i}")
        print(f"事件类型: {log.get('event_type', 'Unknown')}")
        print(f"时间戳: {log.get('timestamp', 'Unknown')}")
        print(f"存储时间: {log.get('stored_at', 'Unknown')}")
        print(f"来源: {log.get('source', 'Unknown')}")
        raw_log = log.get('raw_log', '')
        print(f"原始日志: {raw_log[:100]}{'...' if len(raw_log) > 100 else ''}")
        print("-" * 80)


def show_logs_by_event_type(storage: LogStorage, event_type: str, limit: int = 10):
    """按事件类型显示日志"""
    logs = storage.get_logs_by_event_type(event_type, limit)
    
    print("=" * 80)
    print(f"🏷️  事件类型 '{event_type}' 的日志记录 (最多显示 {limit} 条)")
    print("=" * 80)
    
    if not logs:
        print("暂无记录")
        return
    
    for i, log in enumerate(logs, 1):
        print(f"序号: {i}")
        print(f"时间戳: {log.get('timestamp', 'Unknown')}")
        print(f"存储时间: {log.get('stored_at', 'Unknown')}")
        print(f"来源: {log.get('source', 'Unknown')}")
        print(f"原始日志: {log.get('raw_log', 'Unknown')}")
        print(f"处理数据: {log.get('processed_data', {})}")
        print("-" * 80)


def cleanup_old_logs(storage: LogStorage, days: int):
    """清理旧日志"""
    deleted_count = storage.cleanup_old_logs(days)
    print(f"已清理 {deleted_count} 条 {days} 天前的旧日志")


def main():
    parser = argparse.ArgumentParser(description="日志存储管理工具")
    parser.add_argument('--storage-dir', default='./logs', help='日志存储目录')
    
    subparsers = parser.add_subparsers(dest='command', help='子命令')
    
    # 统计信息命令
    stats_parser = subparsers.add_parser('stats', help='显示存储统计信息')
    
    # 最近日志命令
    recent_parser = subparsers.add_parser('recent', help='显示最近的日志')
    recent_parser.add_argument('--hours', type=int, default=24, help='显示最近几小时的日志')
    
    # 按事件类型查询命令
    type_parser = subparsers.add_parser('type', help='按事件类型查询日志')
    type_parser.add_argument('event_type', help='事件类型')
    type_parser.add_argument('--limit', type=int, default=10, help='返回记录数限制')

    # 清理命令
    cleanup_parser = subparsers.add_parser('cleanup', help='清理旧日志')
    cleanup_parser.add_argument('days', type=int, help='保留天数')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # 初始化存储器
    storage = LogStorage(args.storage_dir)
    
    # 执行相应命令
    if args.command == 'stats':
        show_statistics(storage)
    elif args.command == 'recent':
        show_recent_logs(storage, args.hours)
    elif args.command == 'type':
        show_logs_by_event_type(storage, args.event_type, args.limit)
    elif args.command == 'cleanup':
        cleanup_old_logs(storage, args.days)


if __name__ == '__main__':
    main()