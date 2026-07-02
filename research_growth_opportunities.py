#!/usr/bin/env python3
"""
Growth Opportunities Finder

Uses the full Search Console history to find three separate growth levers:
  1. Keyword count  - content gaps: queries with real demand but no page that
                       actually targets them (net-new keyword/page opportunities)
  2. Traffic        - "quick win" queries sitting at position 4-20, closest to
                       page 1 top-3 where the CTR curve rewards small moves most
  3. Click-through   - queries that already convert some clicks, but at a rate
     rate             far below what's normal for their ranking position

Usage:
    python3 research_growth_opportunities.py [--days 499] [--min-impressions 20]
"""

import argparse
import csv
import os
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

from data_sources.modules.google_search_console import GoogleSearchConsole

MAX_ROWS_PER_PAGE = 25000
MAX_RETRIES = 5

# Approximate organic CTR-by-position benchmarks (industry-average, for flagging only)
EXPECTED_CTR_BY_POSITION = [
    (1, 0.28), (2, 0.15), (3, 0.11), (4, 0.08), (5, 0.07),
    (6, 0.05), (7, 0.04), (8, 0.03), (9, 0.03), (10, 0.025),
    (15, 0.015), (20, 0.01), (30, 0.005), (999, 0.002),
]


def expected_ctr(position: float) -> float:
    for max_pos, ctr in EXPECTED_CTR_BY_POSITION:
        if position <= max_pos:
            return ctr
    return 0.002


def _query_with_retries(gsc: GoogleSearchConsole, request: dict) -> dict:
    for attempt in range(MAX_RETRIES):
        try:
            return gsc.service.searchanalytics().query(
                siteUrl=gsc.site_url, body=request
            ).execute(num_retries=3)
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)


def fetch_all_query_page_rows(gsc: GoogleSearchConsole, days: int) -> list:
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
        response = _query_with_retries(gsc, request)
        rows = response.get('rows', [])
        all_rows.extend(rows)
        print(f'  fetched {len(all_rows):,} rows so far (startRow={start_row})...')
        if len(rows) < MAX_ROWS_PER_PAGE:
            break
        start_row += MAX_ROWS_PER_PAGE

    return all_rows


def aggregate_by_query(rows: list) -> dict:
    by_query = {}
    for row in rows:
        query, page = row['keys']
        entry = by_query.setdefault(query, {'impressions': 0, 'clicks': 0, 'pages': []})
        entry['impressions'] += row['impressions']
        entry['clicks'] += row['clicks']
        entry['pages'].append({
            'page': page,
            'impressions': row['impressions'],
            'clicks': row['clicks'],
            'position': row['position'],
        })
    return by_query


def analyze(by_query: dict, gsc: GoogleSearchConsole, min_impressions: int):
    quick_wins = []       # traffic lever: position 4-20, real impressions
    ctr_underperformers = []  # CTR lever: has clicks, but far below expected for position
    content_gaps = []     # keyword-count lever: no page realistically targets this query

    for query, data in by_query.items():
        if data['impressions'] < min_impressions:
            continue

        data['pages'].sort(key=lambda p: p['impressions'], reverse=True)
        top_page = data['pages'][0]
        avg_position = sum(p['position'] * p['impressions'] for p in data['pages']) / data['impressions']
        avg_position = round(avg_position, 1)
        actual_ctr = data['clicks'] / data['impressions']
        intent_score = gsc._calculate_commercial_intent(query.lower())
        intent_category = gsc._get_intent_category(intent_score)

        # Lever 1: keyword count / content gap - poor position AND poor CTR,
        # meaning no page on the site realistically serves this query yet
        if avg_position > 25 and data['clicks'] == 0:
            content_gaps.append({
                'query': query, 'impressions': data['impressions'], 'avg_position': avg_position,
                'intent_category': intent_category, 'top_page': top_page['page'],
            })
            continue

        # Lever 2: traffic - striking distance of page-1 top 3
        if 4 <= avg_position <= 20:
            exp_ctr = expected_ctr(avg_position)
            potential_clicks_at_top3 = int(data['impressions'] * expected_ctr(3))
            upside = potential_clicks_at_top3 - data['clicks']
            quick_wins.append({
                'query': query, 'impressions': data['impressions'], 'avg_position': avg_position,
                'clicks': data['clicks'], 'actual_ctr': round(actual_ctr * 100, 2),
                'intent_category': intent_category, 'top_page': top_page['page'],
                'click_upside_at_top3': upside,
                'opportunity_score': round(upside * intent_score, 1),
            })

        # Lever 3: CTR - already has clicks, but underperforming its position's benchmark
        if data['clicks'] > 0:
            exp_ctr = expected_ctr(avg_position)
            if actual_ctr < exp_ctr * 0.5 and data['impressions'] >= min_impressions:
                ctr_underperformers.append({
                    'query': query, 'impressions': data['impressions'], 'avg_position': avg_position,
                    'clicks': data['clicks'], 'actual_ctr': round(actual_ctr * 100, 2),
                    'expected_ctr': round(exp_ctr * 100, 2),
                    'intent_category': intent_category, 'top_page': top_page['page'],
                    'missed_clicks': int(data['impressions'] * exp_ctr) - data['clicks'],
                })

    quick_wins.sort(key=lambda o: o['opportunity_score'], reverse=True)
    ctr_underperformers.sort(key=lambda o: o['missed_clicks'], reverse=True)
    content_gaps.sort(key=lambda o: o['impressions'], reverse=True)

    return quick_wins, ctr_underperformers, content_gaps


def cluster_content_gaps_by_page(content_gaps: list) -> list:
    by_page = {}
    for g in content_gaps:
        entry = by_page.setdefault(g['top_page'], {'queries': 0, 'impressions': 0})
        entry['queries'] += 1
        entry['impressions'] += g['impressions']
    clusters = [{'page': page, **data} for page, data in by_page.items()]
    clusters.sort(key=lambda c: c['impressions'], reverse=True)
    return clusters


def write_csv(rows: list, fieldnames: list, path: str):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def write_summary(today: str, total_keywords: int, clicked_keywords: int,
                   quick_wins: list, ctr_underperformers: list,
                   content_gaps: list, clusters: list) -> str:
    path = f'research/growth-opportunities-{today}.md'
    lines = [
        '# Growth Opportunities Report',
        '',
        f'**Generated**: {today}',
        f'**Period analyzed**: last 499 days (Search Console API max)',
        '',
        '## Keyword count baseline',
        '',
        f'- **Total distinct keywords with impressions**: {total_keywords:,}',
        f'- **Keywords earning at least 1 click**: {clicked_keywords:,} ({clicked_keywords/total_keywords*100:.1f}%)',
        f'- **Keywords with zero clicks**: {total_keywords - clicked_keywords:,} ({(total_keywords-clicked_keywords)/total_keywords*100:.1f}%)',
        '',
        '## Lever 1: Keyword count growth (content gaps)',
        '',
        f'{len(content_gaps):,} queries have real demand (impressions) but rank beyond position 25 '
        f'with zero clicks — no page on the site realistically targets them yet. Grouped by the page '
        f'Google currently (weakly) associates with them, to show where new/expanded content would '
        f'capture the most net-new keywords at once:',
        '',
        '| Page | Distinct gap queries | Total impressions |',
        '|------|----------------------|--------------------|',
    ]
    for c in clusters[:25]:
        lines.append(f"| {c['page']} | {c['queries']:,} | {c['impressions']:,} |")

    lines += [
        '',
        f'Full list: `research/growth-content-gaps-{today}.csv`',
        '',
        '## Lever 2: Traffic growth (quick wins, position 4-20)',
        '',
        f'{len(quick_wins):,} queries rank position 4-20 — page 1 or top of page 2, close enough that '
        f'ranking improvements pay off fastest. Sorted by click upside if moved to top 3, weighted by '
        f'commercial intent.',
        '',
        '| # | Query | Impressions | Position | Current CTR | Click upside @top3 | Page |',
        '|---|-------|-------------|----------|-------------|---------------------|------|',
    ]
    for i, o in enumerate(quick_wins[:30], 1):
        lines.append(
            f"| {i} | {o['query']} | {o['impressions']:,} | {o['avg_position']} | "
            f"{o['actual_ctr']}% | +{o['click_upside_at_top3']:,} | {o['top_page']} |"
        )

    lines += [
        '',
        f'Full list: `research/growth-quick-wins-{today}.csv`',
        '',
        '## Lever 3: CTR growth (underperforming their position)',
        '',
        f'{len(ctr_underperformers):,} queries already earn clicks, but at less than half the typical '
        f'CTR for their ranking position — a title/meta mismatch, not a ranking problem.',
        '',
        '| # | Query | Impressions | Position | Actual CTR | Expected CTR | Missed clicks | Page |',
        '|---|-------|-------------|----------|------------|----------------|-----------------|------|',
    ]
    for i, o in enumerate(ctr_underperformers[:30], 1):
        lines.append(
            f"| {i} | {o['query']} | {o['impressions']:,} | {o['avg_position']} | "
            f"{o['actual_ctr']}% | {o['expected_ctr']}% | {o['missed_clicks']:,} | {o['top_page']} |"
        )

    lines += [
        '',
        f'Full list: `research/growth-ctr-underperformers-{today}.csv`',
    ]

    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def main():
    parser = argparse.ArgumentParser(description='Find keyword-count, traffic, and CTR growth opportunities')
    parser.add_argument('--days', type=int, default=499)
    parser.add_argument('--min-impressions', type=int, default=20)
    args = parser.parse_args()

    load_dotenv('data_sources/config/.env')
    gsc = GoogleSearchConsole()
    os.makedirs('research', exist_ok=True)

    print(f'Fetching {args.days} days of query+page data from Search Console...')
    rows = fetch_all_query_page_rows(gsc, args.days)
    by_query = aggregate_by_query(rows)
    total_keywords = len(by_query)
    clicked_keywords = sum(1 for q in by_query.values() if q['clicks'] > 0)
    print(f'{total_keywords:,} distinct keywords, {clicked_keywords:,} earning at least 1 click.')

    quick_wins, ctr_underperformers, content_gaps = analyze(by_query, gsc, args.min_impressions)
    clusters = cluster_content_gaps_by_page(content_gaps)

    today = datetime.now().strftime('%Y-%m-%d')

    write_csv(quick_wins, ['query', 'impressions', 'avg_position', 'clicks', 'actual_ctr',
                           'intent_category', 'top_page', 'click_upside_at_top3', 'opportunity_score'],
              f'research/growth-quick-wins-{today}.csv')
    write_csv(ctr_underperformers, ['query', 'impressions', 'avg_position', 'clicks', 'actual_ctr',
                                     'expected_ctr', 'intent_category', 'top_page', 'missed_clicks'],
              f'research/growth-ctr-underperformers-{today}.csv')
    write_csv(content_gaps, ['query', 'impressions', 'avg_position', 'intent_category', 'top_page'],
              f'research/growth-content-gaps-{today}.csv')

    summary_path = write_summary(today, total_keywords, clicked_keywords,
                                  quick_wins, ctr_underperformers, content_gaps, clusters)

    print(f'Quick wins: {len(quick_wins):,} | CTR underperformers: {len(ctr_underperformers):,} | Content gaps: {len(content_gaps):,}')
    print(f'Summary: {summary_path}')


if __name__ == '__main__':
    main()
