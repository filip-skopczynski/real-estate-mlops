"""Exercise the real structured HTML -> history -> model -> candidate flow."""

import json

import joblib
import numpy as np

from src.database import get_engine, init_db, read_current_listings, read_observations, upsert_listings
from src.fetch_data import parse_listings
from src.preprocess import clean_listings
from src.train import score_listings, train_model
from tests.demo_data import demo_observations


def test_html_to_persisted_model_and_latest_candidates(tmp_path):
    engine = get_engine('sqlite:///' + (tmp_path / 'pipeline.db').as_posix())
    init_db(engine)
    raw = demo_observations()
    for timestamp, observations in raw.groupby('observed_at', sort=True):
        apartments = []
        for row in observations.to_dict(orient='records'):
            apartments.append({
                '@type': 'Apartment', 'identifier': row['listing_id'], 'url': row['url'],
                'address': {'@type': 'PostalAddress', 'addressLocality': row['city']},
                'district': row['district'],
                'floorSize': {'value': row['area_m2'], 'unitCode': 'MTK'},
                'numberOfRooms': row['rooms'], 'floorLevel': row['floor'],
                'yearBuilt': row['build_year'], 'distance_km': row['distance_km'],
                'offers': {'@type': 'Offer', 'price': row['price_pln'], 'priceCurrency': 'PLN',
                           'businessFunction': 'http://purl.org/goodrelations/v1#Sell'},
            })
        html = '<script type="application/ld+json">' + json.dumps(apartments) + '</script>'
        records = parse_listings(html, 'https://demo.invalid/warsaw', timestamp)
        assert len(records) == len(observations)
        assert upsert_listings(engine, records) == len(records)
        assert upsert_listings(engine, records) == 0

    history = read_observations(engine)
    current = read_current_listings(engine)
    engine.dispose()
    assert len(history) == 200
    assert len(current) == 180
    first = clean_listings(history, keep='earliest')
    latest = clean_listings(current, keep='latest')
    artifact, metrics = train_model(first, n_estimators=100)
    model_path = tmp_path / 'model.joblib'
    joblib.dump(artifact, model_path)
    restored = joblib.load(model_path)
    scores = score_listings(latest, restored)
    assert len(scores) == 180
    assert np.isfinite(scores['predicted_price_pln']).all()
    assert metrics['xgboost']['mae_pln'] < metrics['baseline_median']['mae_pln']
    assert scores['is_deal'].any()
    assert scores['partition'].isin(['train', 'validation', 'test', 'unseen']).all()
    np.testing.assert_allclose(scores['discount'],
        (scores['predicted_price_pln'] - scores['price_pln']) / scores['predicted_price_pln'])
    assert (scores.loc[scores['is_deal'], 'discount'] >= 0.15).all()
