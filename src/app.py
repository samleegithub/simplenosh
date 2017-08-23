from flask import Flask, request, render_template, jsonify
import pickle
import numpy as np
import pyspark as ps
import pyspark.sql.functions as F
import pyspark.sql.types as T

app = Flask(__name__)
PORT = 5353

# with open('../data/item_factors.pkl', 'rb') as f:
#     item_factors = pickle.load(f)

# with open('../data/item_ids.pkl', 'rb') as f:
#     item_ids = pickle.load(f)

spark = (
    ps.sql.SparkSession.builder
    .config('spark.executor.memory', '1g')
    # .master("local[8]")
    .appName("webapp")
    .getOrCreate()
)

# Load restaurant metadata
restaurants_df = spark.read.parquet('../data/restaurants')

# Load restaurant discount factors
# discount_factor_df = spark.read.parquet('../data/discount_factor')

# Load restaurant ids into mapping dataframe
restaurant_id_map = []
index = 0
with open('../data/product_labels.txt') as f:
    for line in f:
        restaurant_id = line.strip()
        restaurant_id_map.append((restaurant_id, index))
        index += 1

restaurant_id_map_df = spark.createDataFrame(restaurant_id_map, ['id', 'item'])

restaurants_with_id_df = restaurants_df.join(restaurant_id_map_df, on='id')

# Load rating_stats calculated from model's training data
rating_stats_df = spark.read.parquet('../data/rating_stats')

# Load item_bias calculated from model's training data
item_bias_df = spark.read.parquet('../data/item_bias')

# Load residual_stats that generated by model from training data
residual_stats_df = spark.read.parquet('../data/residual_stats')

# Load item_factors generated by model from training data
item_factors_df = spark.read.parquet('../data/item_factors')


def get_item_factors_and_ids():
    item_factors = []
    item_ids = []
    for row in item_factors_df.collect():
        item_factors.append(row['features'])
        item_ids.append(row['id'])
    item_factors = np.array(item_factors)
    item_ids = np.array(item_ids)
    return item_factors, item_ids


# Setup item_factors and item_ids. Static values so need to load only once.
item_factors, item_ids = get_item_factors_and_ids()


def find_str_in_categories(categories, keyword):
    for row in categories:
            if keyword in row['alias'].lower():
                return True
    return False


find_str_in_categories_udf = F.udf(find_str_in_categories, T.BooleanType())


@app.route('/search', methods=['POST'])
def search():
    # keyword = str(request.form['keyword']).lower()
    # location = str(request.form['location']).lower()

    json_data = request.get_json()

    keyword = str(json_data['keyword']).lower()
    location = str(json_data['location']).lower()

    results_data = (
        restaurants_with_id_df
        .filter(
            (
                F.lower(F.col('name')).like('%{}%'.format(keyword))
                | find_str_in_categories_udf(F.col('categories'), F.lit(keyword))
            )
            & (
                F.lower(F.col('location.city')).like('%{}%'.format(location))
                | F.lower(F.col('location.address1')).like('%{}%'.format(location))
                | F.lower(F.col('location.address2')).like('%{}%'.format(location))
                | F.lower(F.col('location.address3')).like('%{}%'.format(location))
                | F.lower(F.col('location.zip_code')).like('%{}%'.format(location))
                | F.lower(F.col('location.state')).like('%{}%'.format(location))
            )
        )
        .collect()
    )
    
    results = {}
    for row in results_data:
        results[row['id']] = {
            'model_id': row['item'],
            'name': row['name'],
            'url': row['url'],
            'image_url': row['image_url'],
            'location': row['location'],
            'rating': row['rating'],
            'categories': row['categories']
        }

    # restaurants_with_id_df.printSchema()

    # restaurants_df.printSchema()
    return jsonify(results)


def get_user_factors(user_ratings_df):
    filtered_item_factors_df = (
        item_factors_df
        .join(user_ratings_df, F.col('id') == user_ratings_df['item'])
        .crossJoin(rating_stats_df)
        .join(item_bias_df, on='item')
        .withColumn(
            'orig_rating',
            F.col('rating')
        )
        .withColumn(
            'rating',
            F.col('rating')
            - F.col('avg_rating')
            - F.col('item_bias')
        )
    )

    # filtered_item_factors_df.printSchema()
    # print(filtered_item_factors_df.show(100))

    filtered_item_factors = []
    item_ratings = []
    for row in filtered_item_factors_df.collect():
        filtered_item_factors.append(row['features'])
        item_ratings.append(row['rating'])
    filtered_item_factors = np.array(filtered_item_factors)
    item_ratings = np.array(item_ratings)

    # print('filtered_item_factors')
    # print(filtered_item_factors)

    # print('item_ratings')
    # print(item_ratings)

    return np.dot(item_ratings, filtered_item_factors) / len(item_ratings)


def make_new_user_predictions(user_ratings_df):
    user_factors = get_user_factors(user_ratings_df)

    # print('user_factors: {}'.format(user_factors))

    predictions = np.dot(user_factors, item_factors.T)

    prediction_df = spark.createDataFrame(
        zip(item_ids.tolist(), predictions.tolist()),
        ['item', 'res_prediction']
    )

    res_prediction_stats_df = (
        prediction_df
        .agg(
            F.avg(F.col('res_prediction')).alias('avg_res_prediction'),
            F.stddev_samp(F.col('res_prediction')).alias('stddev_res_prediction')
        )
    )

    predicted_rating_df = (
        prediction_df
        .crossJoin(rating_stats_df)
        .crossJoin(res_prediction_stats_df)
        .crossJoin(residual_stats_df)
        .join(item_bias_df, on='item')
        .withColumn(
            'prediction',
            (
                (
                    F.col('res_prediction')
                    # - F.col('avg_res_prediction')
                )
                # * F.col('stddev_residual')
                # / F.col('stddev_res_prediction')
                # + F.col('avg_residual')
                # + F.col('avg_rating')
                + F.col('item_bias')
            )
            * (1 - 1 / F.sqrt(F.col('count_item_rating')))
        )
        .filter(F.col('prediction') > 0)
    )

    predicted_rating_stats_df = (
        predicted_rating_df
        .agg(
            F.avg(F.col('prediction')).alias('avg_prediction'),
            F.stddev_samp(F.col('prediction')).alias('stddev_prediction')
        )
    )

    # print('prediction_df')
    # prediction_df.show()

    # print('predicted_rating_df')
    # predicted_rating_df.show()

    print('residual_stats_df')
    residual_stats_df.show()

    print('res_prediction_stats_df')
    res_prediction_stats_df.show()

    print('rating_stats_df')
    rating_stats_df.show()

    print('predicted_rating_stats_df')
    predicted_rating_stats_df.show()

    return predicted_rating_df


@app.route('/recommend', methods=['POST'])
def recommend():
    json_data = request.get_json()

    user_ratings = json_data['user_ratings']
    keyword = str(json_data['keyword']).lower()
    location = str(json_data['location']).lower()

    user_ratings_data = list(user_ratings.items())

    # Define schema
    schema = T.StructType([
        T.StructField('item', T.StringType(), True),
        T.StructField('rating', T.StringType(), True)
    ])

    user_ratings_df = spark.createDataFrame(user_ratings_data, schema=schema)

    user_ratings_df = (
        user_ratings_df
        .select(
            F.col('item').cast(T.IntegerType()).alias('item'),
            F.col('rating').cast(T.ByteType()).alias('rating')
        )
    )

    # user_ratings_df.printSchema()
    # print(user_ratings_df.head(10))

    predicted_rating_df = make_new_user_predictions(user_ratings_df)

    prediction_data_df = (
        predicted_rating_df
        .join(restaurants_with_id_df, on='item')
        .filter(
            (
                F.lower(F.col('name')).like('%{}%'.format(keyword))
                | find_str_in_categories_udf(F.col('categories'), F.lit(keyword))
            )
            & (
                F.lower(F.col('location.city')).like('%{}%'.format(location))
                | F.lower(F.col('location.address1')).like('%{}%'.format(location))
                | F.lower(F.col('location.address2')).like('%{}%'.format(location))
                | F.lower(F.col('location.address3')).like('%{}%'.format(location))
                | F.lower(F.col('location.zip_code')).like('%{}%'.format(location))
                | F.lower(F.col('location.state')).like('%{}%'.format(location))
            )
        )
        .join(
            user_ratings_df
            .select(
                F.col('item'),
                F.col('rating').alias('user_rating')
            ),
            on='item',
            how='left_outer'
        )
        .filter(F.isnull('user_rating'))
        .sort(F.col('prediction'), ascending=False)
    )

    # prediction_data_df.printSchema()
    # print(prediction_data_df.show(20))


    results = {}
    for i, row in enumerate(prediction_data_df.take(25)):
        results[i] = {
            'model_id': row['item'],
            'prediction': row['prediction'],
            'name': row['name'],
            'url': row['url'],
            'image_url': row['image_url'],
            'location': row['location'],
            'rating': row['rating'],
            'count_item_rating': row['count_item_rating'],
            'item_bias': row['item_bias'],
            'res_prediction': row['res_prediction'],
            'categories': row['categories']
        }


    return jsonify(results)


@app.route('/')
def index():
    return render_template('index.html')


def main():    
    # Start Flask app
    app.run(host='0.0.0.0', port=PORT, debug=True, threaded=True)


if __name__ == '__main__':
    main()
