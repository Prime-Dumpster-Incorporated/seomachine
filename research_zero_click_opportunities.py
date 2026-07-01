#!/usr/bin/env python3
"""
Zero-Click Opportunity Finder

Finds Search Console queries that generate impressions but zero clicks,
matches each to whichever page Google is currently showing for it, and
classifies the gap so it can feed into /analyze-existing, /rewrite, or
/research + /write.

Usage:
    python3 research_zero_click_opportunities.py [--days 499] [--min-impressions 50] [--limit 100]
"""

import argparse
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

from data_sources.modules.google_search_console import GoogleSearchConsole

MAX_ROWS_PER_PAGE = 25000


def fetch_all_query_page_rows(gsc: GoogleSearchConsole, days: int) -> list:
    """Pull every (query, page) row for the period, paginating past the 25k row cap."""
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    end_date = datetime.now().strftime('%Y-%m-%d')

    all_rows = []
    start_row = 0
    while True:
        request = {
            'startDate': start_date,
            'endDate': end_date,
            'dimensions': ['query', 'page'],
            'rowLimit': MAX_ROWS_PER_PAGE,
            'startRow': start_row,
        }
        response = gsc.service.searchanalytics().query(
            siteUrl=gsc.site_url, body=request
        ).execute()
        rows = response.get('rows', [])
        all_rows.extend(rows)
        if len(rows) < MAX_ROWS_PER_PAGE:
            break
        start_row += MAX_ROWS_PER_PAGE

    return all_rows


def find_zero_click_queries(gsc: GoogleSearchConsole, days: int, min_impressions: int) -> list:
    rows = fetch_all_query_page_rows(gsc, days)

    by_query = {}
    for row in rows:
        query, page = row['keys']
        entry = by_query.setdefault(query, {'impressions': 0, 'clicks': 0, 'pages': []})
        entry['impressions'] += row['impressions']
        entry['clicks'] += row['clicks']
        entry['pages'].append({
            'page': page,
            'impressions': row['impressions'],
            'position': round(row['position'], 1),
        })

    opportunities = []
    for query, data in by_query.items():
        if data['clicks'] != 0 or data['impressions'] < min_impressions:
            continue

        data['pages'].sort(key=lambda p: p['impressions'], reverse=True)
        top_page = data['pages'][0]
        avg_position = round(
            sum(p['position'] * p['impressions'] for p in data['pages']) / data['impressions'], 1
        )

        intent_score = gsc._calculate_commercial_intent(query.lower())
        intent_category = gsc._get_intent_category(intent_score)

        if avg_position > 20:
            action = 'Content gap likely — page barely visible for this query. Consider new/targeted content.'
        else:
            action = 'Page ranks but isn\'t earning clicks — rewrite title/meta to match this query intent.'

        opportunities.append({
            'query': query,
            'impressions': data['impressions'],
            'avg_position': avg_position,
            'intent_score': intent_score,
            'intent_category': intent_category,
            'top_page': top_page['page'],
            'other_pages': [p['page'] for p in data['pages'][1:3]],
            'action': action,
        })

    # Prioritize by impressions weighted by commercial intent
    opportunities.sort(key=lambda o: o['impressions'] * o['intent_score'], reverse=True)
    return opportunities


def write_report(opportunities: list, days: int, min_impressions: int, limit: int) -> str:
    today = datetime.now().strftime('%Y-%m-%d')
    os.makedirs('research', exist_ok=True)
    report_path = f'research/gsc-zero-click-opportunities-{today}.md'

    lines = [
        f'# Zero-Click Opportunities Report',
        f'',
        f'**Generated**: {today}  ',
        f'**Period analyzed**: last {days} days  ',
        f'**Min impressions threshold**: {min_impressions}  ',
        f'**Total zero-click queries found**: {len(opportunities)}',
        f'',
        f'Queries below get impressions in Google Search but zero clicks. Sorted by '
        f'impressions weighted by commercial intent, so the highest-value gaps are first.',
        f'',
        f'| # | Query | Impressions | Avg Position | Intent | Ranking Page | Action |',
        f'|---|-------|-------------|---------------|--------|---------------|--------|',
    ]

    for i, o in enumerate(opportunities[:limit], 1):
        lines.append(
            f"| {i} | {o['query']} | {o['impressions']:,} | {o['avg_position']} | "
            f"{o['intent_category']} | {o['top_page']} | {o['action']} |"
        )

    lines += [
        '',
        '## Next Steps',
        '',
        '- **"Page ranks but isn\'t earning clicks"** rows: run `/analyze-existing [page]` then '
        'fix title/meta with the Meta Creator agent — fastest wins, no new content needed.',
        '- **"Content gap likely"** rows: run `/research [query]` then `/write [query]` to build '
        'dedicated content targeting that query.',
        '- Re-run this script monthly to track whether fixes are converting impressions into clicks.',
    ]

    with open(report_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    return report_path


def main():
    parser = argparse.ArgumentParser(description='Find GSC queries with impressions but zero clicks')
    parser.add_argument('--days', type=int, default=499, help='Days of history to analyze (GSC API max ~16 months)')
    parser.add_argument('--min-impressions', type=int, default=50, help='Minimum impressions to include a query')
    parser.add_argument('--limit', type=int, default=200, help='Max rows to include in the report')
    args = parser.parse_args()

    load_dotenv('data_sources/config/.env')
    gsc = GoogleSearchConsole()

    print(f'Fetching {args.days} days of query+page data from Search Console...')
    opportunities = find_zero_click_queries(gsc, args.days, args.min_impressions)
    print(f'Found {len(opportunities)} zero-click queries with {args.min_impressions}+ impressions.')

    report_path = write_report(opportunities, args.days, args.min_impressions, args.limit)
    print(f'Report saved to: {report_path}')


if __name__ == '__main__':
    main()
