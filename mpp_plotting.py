from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from textwrap import dedent

from IPython.display import display
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pandas.io.sql as psql
import psycopg2
import seaborn as sns
import sqlalchemy
from sqlalchemy import create_engine, Column, MetaData, Table
from sqlalchemy import all_, and_, any_, not_, or_
from sqlalchemy import alias, between, case, cast, column, distinct, extract,\
                       false, func, intersect, literal, literal_column,\
                       select, text, true, union, union_all
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, Float,\
                       Numeric, String



def _add_weights_column(df_list, normed):
    """Add the weights column for each DataFrame in df_list."""
    for df in df_list:
        df['weights'] = _create_weight_percentage(df[['freq']], normed)


def _create_weight_percentage(hist_col, normed=False):
    """Convert frequencies to percent."""
    if normed:
        return hist_col/hist_col.sum()
    else:
        return hist_col


def _get_bin_locs_numeric(nbins, col_val, min_val, max_val):
    """Gets the bin locations for a numeric type."""
    # Which bin it should fall into
    numer = (col_val - min_val).cast(Numeric)
    denom = (max_val - min_val).cast(Numeric)
    bin_nbr = func.floor(numer/denom * nbins)

    # Group max value into the last bin. It would otherwise be in a
    # separate bin on its own.
    bin_nbr_correct = case([(bin_nbr < nbins, bin_nbr)],
                           else_=bin_nbr - 1
                          )

    # Scale the bins to their proper size
    bin_nbr_scaled = bin_nbr_correct/nbins * denom
    # Translate bins to their proper locations
    bin_loc = bin_nbr_scaled + min_val

    return bin_loc


def _get_bin_locs_time(nbins, col_val, min_val, max_val):
    """Gets the bin locations for a time type."""
    # Get the SQL expressions for the time ranges
    numer = func.extract('EPOCH', col_val - max_val).cast(Numeric)
    denom = func.extract('EPOCH', min_val - max_val).cast(Numeric)

    # Which bin it should fall into
    bin_nbr = func.floor(numer/denom * nbins)
    # Group max value into the last bin. It would otherwise be in a
    # separate bin on its own
    bin_nbr_correct = case([(bin_nbr < nbins, bin_nbr)],
                           else_=bin_nbr - 1
                          )
    # Scale the bins to their proper size
    bin_nbr_scaled = bin_nbr_correct/nbins * denom
    # Translate bins to their proper locations
    bin_loc = bin_nbr_scaled * text("INTERVAL '1 second'") + min_val

    return bin_loc


def _get_min_max_alias(from_obj, column_name, alias_name, min_val_name,
                       max_val_name):
    """Returns a SQLAlchemy alias that captures the min and max values
    of a column.
    """

    min_max_alias =\
        select([func.min(column(column_name)).label(min_val_name),
                func.max(column(column_name)).label(max_val_name)
               ],
               from_obj=from_obj
               )\
        .alias(alias_name)

    return min_max_alias


def _is_category_column(from_obj, column_name):
    """Returns whether the column is a category."""
    data_type = from_obj.c[column_name].type.__visit_name__
    numeric_types = ['BIGINT', 'DATE', 'DOUBLE PRECISION', 'INT',
                     'INTEGER', 'FLOAT', 'NUMERIC', 'TIMESTAMP',
                     'TIMESTAMP WITHOUT TIME ZONE']
    return data_type not in numeric_types


def _is_time_type(from_obj, column_name):
    """Returns whether the column is a time type (date or timestamp)."""
    data_type = from_obj.c[column_name].type.__visit_name__
    time_types = ['DATE', 'TIMESTAMP', 'TIMESTAMP WITHOUT TIME ZONE']
    return data_type in time_types


def _listify(df_list, labels):
    """If df_list and labels are DataFrames and strings respectively,
    make them into lists to conform with the rest of the code as it is
    built to handle multiple variables.
    """

    if isinstance(df_list, pd.DataFrame):
        df_list = [df_list]
    if isinstance(labels, str):
        labels = [labels]
    return df_list, labels



def get_histogram_values(data, column_name, engine, schema=None, nbins=25,
                         bin_width=None, cast_as=None, print_query=False):
    """Takes a SQL table and creates histogram bin heights. Relevant
    parameters are either the number of bins or the width of each bin.
    Only one of these is specified. The other one must be left at its
    default value of 0 or it will throw an error.
    
    Parameters
    ----------
    data : str or SQLAlchemy selectable
        The table we wish to compute a histogram with
    column_name : str
        Name of the column of interest
    engine : SQLAlchemy engine object
    schema : str, default None
        The name of the schema where data is found
    nbins : int, default 25
        Number of desired bins
    bin_width : int, default None
        Width of each bin. If None, then use nbins to define bin width.
    cast_as : SQLAlchemy data type, default None
        SQL type to cast as
    print_query : boolean, default False
        If True, print the resulting query
    """

    def _check_for_input_errors(nbins, bin_width):
        """Check to see if any inputs conflict and raise an error if
        there are issues.
        """

        if nbins is not None and nbins < 0:
            raise Exception('nbins must be positive.')
        if bin_width is not None and bin_width < 0:
            raise Exception('bin_width must be positive.')

    if schema is not None and not isinstance(data, str):
        raise ValueError('schema cannot be specified unless data is of string '
                         'type.')
    if isinstance(data, str):
        metadata = MetaData(engine)
        data = Table(data, metadata, autoload=True, schema=schema)

    _check_for_input_errors(nbins, bin_width)
    is_category = _is_category_column(data, column_name)
    is_time_type = _is_time_type(data, column_name)

    if is_category:
        binned_slct =\
            select([column(column_name).label('category'),
                    func.count('*').label('freq')
                   ],
                   from_obj=data
                  )\
            .group_by(column_name)\
            .order_by(column_name)
    else:
        # Get column variables
        min_val = column('min_val')
        max_val = column('max_val')
        col_val = column(column_name)
        
        # Table to get min and max value
        min_max_alias = _get_min_max_alias(data,
                                           column_name,
                                           'min_max_table',
                                           min_val.name,
                                           max_val.name
                                          )

        if bin_width is not None:
            # If bin width is not specified, calculate nbins from it.
            nbins = (max_val - min_val)/bin_width

        if is_time_type:
            bin_loc = _get_bin_locs_time(nbins, col_val, min_val, max_val)
        else:
            bin_loc = _get_bin_locs_numeric(nbins, col_val, min_val, max_val)

        # Group by the bin locations
        binned_slct =\
            select([bin_loc.label('bin_loc'),
                    func.count('*').label('freq')
                   ], 
                   from_obj=[data, min_max_alias]
                  )\
            .group_by('bin_loc')\
            .order_by('bin_loc')

    if print_query:
        print binned_slct

    return psql.read_sql(binned_slct, engine)


def get_roc_auc_score(roc_df, tpr_column='tpr', fpr_column='fpr'):
    """Given an ROC DataFrame such as the one created in get_roc_curve,
    return the AUC. This is achieved by taking the ROC curve and 
    interpolating every single point with a straight line and computing
    the sum of the areas of all the trapezoids.

    Parameters
    ----------
    roc_df : DataFrame
        Contains the columns for true positive and false positive rates
    tpr_column : str, default 'tpr'
        Name of the true positive rate column
    fpr_column : str, default 'fpr'
        Name of the false positive rate column

    Returns
    -------
    auc_val : float
    """

    # The average of the two consecutive tprs
    avg_height = roc_df[tpr_column].rolling(2).mean()[1:]
    # The width (i.e., distance between two consecutive fprs)
    width = roc_df[fpr_column].diff()[1:]

    auc_val = sum(avg_height * width)
    return auc_val


def get_roc_curve(data, y_true, y_score, engine, schema=None,
                  print_query=False):
    """Computes the ROC curve in database.

    Parameters
    ----------
    data : str or SQLAlchemy selectable
        The table we wish to compute a histogram with
    y_true : str
        Name of the column that contains the true values
    y_score: str
        Name of the column that contains the scores from the machine
        learning algorithm
    engine : SQLAlchemy engine object
    schema : str, default None
        The name of the schema where data is found
    print_query : boolean, default False
        If True, print the resulting query

    Returns
    -------
    roc_df : DataFrame
    """

    if schema is not None and not isinstance(data, str):
        raise ValueError('schema cannot be specified unless data is of string '
                         'type.')
    if isinstance(data, str):
        metadata = MetaData(engine)
        data = Table(data, metadata, autoload=True, schema=schema)

    y_true_col = column(y_true)
    y_score_col = column(y_score)

    # Add row numbers
    row_nbr_alias =\
        select([func.row_number()
                    .over(order_by=y_score_col)
               ] + list(data.c)
              )\
        .alias('row_nbr')

    # Calculate number of positives and negatives past a given threshold
    pre_roc_alias =\
        select(row_nbr_alias.c
               + [func.sum(y_true_col)
                      .over(order_by=y_score_col.desc())
                      .label('num_pos'),
                  func.sum(1 - y_true_col)
                      .over(order_by=y_score_col.desc())
                      .label('num_neg')
                 ]
              )\
        .alias('pre_roc')

    # Get the sizes of the positive and negative classes
    class_sizes_alias =\
        select([func.sum(y_true_col).label('tot_pos'),
                func.sum(1 - y_true_col).label('tot_neg')
               ],
               from_obj=data
              )\
        .alias('class_sizes')

    # Compute ROC curve values
    roc_slct =\
        select([distinct(y_score_col).label('thresholds'),
                (column('num_pos')/column('tot_pos').cast(Numeric))
                    .label('tpr'),
                (column('num_neg')/column('tot_neg').cast(Numeric))
                    .label('fpr')
               ],
               from_obj=[pre_roc_alias, class_sizes_alias]
              )\
        .order_by('tpr', 'fpr')

    roc_df = psql.read_sql(roc_slct, engine)
    return roc_df


def get_scatterplot_values(data, column_name_x, column_name_y, engine,
                           schema=None, nbins=(50, 50), bin_size=None,
                           cast_x_as=None, cast_y_as=None, print_query=False):
    """Takes a SQL table and creates scatter plot bin values. This is
    the 2D version of get_histogram_values. Relevant parameters are
    either the number of bins or the size of each bin in both the x and
    y direction. Only number of bins or size of the bins is specified.
    The other pair must be left at its default value of 0 or it will
    throw an error.
    
    Parameters
    ----------
    data : str or SQLAlchemy selectable
        The table we wish to compute a histogram with
    column_name_x : str
        Name of one column of interest to be plotted
    column_name_t : str
        Name of another column of interest to be plotted
    engine : SQLAlchemy engine object, default None
    schema : str, default None
        The name of the schema where data is found
    nbins : tuple, default (50, 50)
        Number of desird bins for x and y directions
    bin_size : tuple, default None
        The size of of the bins for the x and y directions
    print_query : boolean, default False
        If True, print the resulting query

    Returns
    -------
    scatterplot_df : DataFrame
    """

    def _check_for_input_errors(nbins, bin_size):
        """Check to see if any inputs conflict and raise an error if
        there are issues.
        """

        if bin_size is not None:
            if bin_size[0] < 0 or bin_size[1] < 0:
                raise Exception('Bin dimensions must both be positive.')
        elif nbins is not None:
            if nbins[0] < 0 or nbins[1] < 0:
                raise Exception('Number of bin dimensions must both be positive')

    def _get_bin_loc_tbl(min_max_tbl, nbins, bin_name, min_val, max_val):
        """Gets all bin locations for a numeric type, including for bins
        that do not contain any data. This is used for scatter plot
        heatmaps where we will need to fill it in. Regular scatter plots
        do not need since this we perform a simple group by.
        """

        bin_range = max_val - min_val
        bin_loc = column('bin_nbr').cast(Numeric)/nbins * bin_range + min_val

        bin_loc_tbl =\
            select([bin_loc.cast(Numeric).label(bin_name)],
                   from_obj=[func.generate_series(1, nbins).alias('bin_nbr'),
                             min_max_tbl
                            ]
                  )

        return bin_loc_tbl

    def _get_scat_bin_tbl(bin_loc_tbl_x, bin_loc_tbl_y):
        """Gets the scatter plot bin location pairs."""

        bin_loc_tbl_x_alias = bin_loc_tbl_x.alias('bin_loc_x')
        bin_loc_tbl_y_alias = bin_loc_tbl_y.alias('bin_loc_y')

        scat_bin_tbl =\
            select(bin_loc_tbl_x_alias.c + bin_loc_tbl_y_alias.c,
                   from_obj=[bin_loc_tbl_x_alias,
                             bin_loc_tbl_y_alias
                            ]
                  )\
            .alias('scat_bin_tbl')

        return scat_bin_tbl

    if schema is not None and not isinstance(data, str):
        raise ValueError('schema cannot be specified unless data is of string type.')
    if isinstance(data, str):
        metadata = MetaData(engine)
        data = Table(data, metadata, autoload=True, schema=schema)

    _check_for_input_errors(nbins, bin_size)
    is_category_x = _is_category_column(data, column_name_x)
    is_category_y = _is_category_column(data, column_name_y)
    is_time_type_x = _is_time_type(data, column_name_x)
    is_time_type_y = _is_time_type(data, column_name_y)

    if is_category_x and is_category_y:
        binned_table =\
            select([column(column_name_x).label('category_x'),
                    column(column_name_y).label('category_y'),
                    func.count('*').label('freq')
                   ],
                   from_obj=data
                   )\
            .group_by(column_name_x, column_name_y)\
            .order_by(column_name_x, column_name_y)

        if print_query:
            print binned_table

        return psql.read_sql(binned_table, engine)

    elif not is_category_x and not is_category_y:
        min_val_x = column('min_val_x')
        max_val_x = column('max_val_x')
        col_val_x = column(column_name_x)

        min_val_y = column('min_val_y')
        max_val_y = column('max_val_y')
        col_val_y = column(column_name_y)

        min_max_tbl_x = _get_min_max_alias(data,
                                           column_name_x,
                                           'min_max_table_x',
                                           min_val_x.name,
                                           max_val_x.name
                                          )
        min_max_tbl_y = _get_min_max_alias(data,
                                           column_name_y,
                                           'min_max_table_y',
                                           min_val_y.name,
                                           max_val_y.name
                                          )

        if bin_size is not None:
            # If bin size is not specified, calculated nbins_x and
            # nbins_y from it.
            nbins[0] = (max_val_x - min_val_x)/bin_size[0]
            nbins[1] = (may_val_y - min_val_y)/bin_size[1]

        if is_time_type_x:
            bin_loc_x = _get_bin_locs_time(nbins[0], col_val_x,
                                           min_val_x, max_val_x)
        else:
            bin_loc_x = _get_bin_locs_numeric(nbins[0], col_val_x,
                                              min_val_x, max_val_x)

        if is_time_type_y:
            bin_loc_y = _get_bin_locs_time(nbins[1], col_val_y,
                                           min_val_y, max_val_y)
        else:
            bin_loc_y = _get_bin_locs_numeric(nbins[1], col_val_y,
                                              min_val_y, max_val_y)

        binned_table =\
            select([bin_loc_x.cast(Numeric).label('bin_loc_x'),
                    bin_loc_y.cast(Numeric).label('bin_loc_y'),
                    func.count('*').label('freq')
                   ],
                   from_obj=[data, min_max_tbl_x, min_max_tbl_y]
                   )\
            .group_by('bin_loc_x', 'bin_loc_y')\

        bin_loc_tbl_x = _get_bin_loc_tbl(min_max_tbl_x,
                                         nbins[0],
                                         'scat_bin_x',
                                         min_val_x,
                                         max_val_x
                                        )
        bin_loc_tbl_y = _get_bin_loc_tbl(min_max_tbl_y,
                                         nbins[1],
                                         'scat_bin_y',
                                         min_val_y,
                                         max_val_y
                                        )
        scat_bin_tbl = _get_scat_bin_tbl(bin_loc_tbl_x, bin_loc_tbl_y)
        
        join_table =\
            scat_bin_tbl.alias('scat_bin_table')\
            .join(binned_table.alias('binned_table'),
                  isouter=True,
                  onclause=and_(column('bin_loc_x') == column('scat_bin_x'),
                                column('bin_loc_y') == column('scat_bin_y')
                               )
                 )

        scatterplot_tbl =\
            select([column('scat_bin_x'),
                    column('scat_bin_y'),
                    func.coalesce(column('freq'), 0).label('freq')
                   ],
                   from_obj=join_table
                  )
    
        if print_query:
            print scatterplot_tbl

        scatterplot_df = psql.read_sql(scatterplot_tbl, engine)
        return scatterplot_df


def plot_categorical_hists(df_list, labels=[], log=False, normed=False,
                           null_at='left', order_by=0, ascending=True,
                           color_palette=sns.color_palette('deep')):
    """Plots categorical histograms.
    
    Parameters
    ----------
    df_list : A DataFrame or a list of DataFrames
        DataFrame or list of DataFrames which have two columns
        category and freq). Category is the unique value of the column
        and the frequency is how many values fall in that bin.
    labels : str or list of str
        A string (for one histogram) or list of strings which sets the
        labels for the histograms
    log : bool, default False
        Whether to display y axis on log scale
    normed : bool, default False
        Whether to normalize histograms so that the heights of each bin
        sum up to 1. This is useful for plotting columns with different
        sizes
    null_at : str, default 'order'
        Which side to set a null value column. The options are:
            'left' - Put the null on the left side
            'right' - Put it on the right side
            '' - If left blank, leave out
    order_by : {'alphabetical', int}, default 0
        How to order the bars. The options are:
            'alphabetical' - Orders the categories in alphabetical order
            integer - An integer value denoting for which df_list
                DataFrame to sort by
    ascending : bool, default False
        Whether to sort values in ascending order
    color_palette : list of tuples, default sns deep colour palette
        Seaborn colour palette, i.e., a list of tuples representing the
        colours.
    """

    def _join_freq_df(df_list):
        """Joins all the DataFrames so that we have a master table with
        category and the frequencies for each table.

        Returns the joined DataFrame
        """

        for i in xrange(len(df_list)):
            temp_df = df_list[i].copy()
            temp_df.columns = ['category', 'freq_{}'.format(i)]

            # Add weights column (If normed, we must take this into account)
            weights_col = 'weights_{}'.format(i)
            freq_col = 'freq_{}'.format(i)
            temp_df[weights_col] = _create_weight_percentage(temp_df[freq_col],
                                                             normed)

            if i == 0:
                df = temp_df
            else:
                df = pd.merge(df, temp_df, how='outer', on='category')

        # Fill in nulls with 0 (except for category column)
        for col in df.columns[1:]:
            df[col] = df[col].fillna(0)
        return df
  
    def _get_num_categories(hist_df):
        """Get the number of categories depending on whether we are 
        specifying to drop it in the function.
        """

        if null_at == '':
            return hist_df['category'].dropna().shape[0]
        else:
            return hist_df.shape[0]
   
    def _get_bin_order(loc, hist_df, order_by):
        """Sorts hist_df by the specified order."""
        if order_by == 'alphabetical':
            return hist_df\
                .sort_values('category', ascending=ascending)\
                .reset_index(drop=True)
        elif isinstance(order_by, int):
            # Desired column in the hist_df DataFrame
            weights_col = 'weights_{}'.format(order_by)

            if weights_col not in hist_df.columns:
                raise Exception('order_by index not in hist_df.')
            return hist_df\
                .sort_values(weights_col, ascending=ascending)\
                .reset_index(drop=True)
        else:
            raise Exception('Invalid order_by')

    def _get_bin_left(loc, hist_df):
        """Returns a list of the locations of the left edges of the
        bins.
        """
        
        def _get_within_bin_left(hist_df):
            """Each bin has width 1. If there is more than one
            histogram, each one must fit in this bin of width 1, so

            Returns indices within a bin for each histogram.
            """

            if len(df_list) == 1:
                return [0, 1]
            else:
                return np.linspace(0.1, 0.9, num_hists + 1)[:-1]

        within_bin_left = _get_within_bin_left(hist_df)

        # For each histogram, we generate a separate list of tick
        # locations. We do this so that later, when we plot we can use
        # different colours for each.

        # If there are any NULL categories
        if np.sum(hist_df.category.isnull()) > 0:
            if loc == 'left': 
                bin_left = [np.arange(1 + within_bin_left[i], num_categories + within_bin_left[i]).tolist() for i in range(num_hists)]
                null_left = [[within_bin_left[i]] for i in range(num_hists)]
            elif loc == 'right':
                bin_left = [np.arange(within_bin_left[i], num_categories - 1 + within_bin_left[i]).tolist() for i in range(num_hists)]
                # Subtract one from num_categories since num_categories
                # includes the null bin. Subtracting will place the null 
                # bin in the proper location.
                null_left = [[num_categories - 1 + within_bin_left[i]] for i in range(num_hists)]
            elif loc == 'order':
                # Get the index of null and non-null categories in
                # hist_df
                null_indices = np.array(hist_df[pd.isnull(hist_df.category)].index)
                non_null_indices = np.array(hist_df.dropna().index)
                bin_left = [(within_bin_left[i] + non_null_indices).tolist() for i in range(num_hists)]
                null_left = [(within_bin_left[i] + null_indices).tolist() for i in range(num_hists)]
            elif loc == '':
                bin_left = [np.arange(within_bin_left[i], num_categories + 1 + within_bin_left[i])[:-1].tolist() for i in range(num_hists)]
                null_left = [[]] * num_hists
        else:
            bin_left = [np.arange(within_bin_left[i], hist_df.dropna().shape[0] + 1 + within_bin_left[i])[:-1].tolist() for i in range(num_hists)]
            null_left = [[]] * num_hists

        return bin_left, null_left

    def _get_bin_height(loc, order_by, hist_df):
        """Returns a list of the heights of the bins and the category
        order.
        """

        hist_df_null = hist_df[hist_df.category.isnull()]
        hist_df_non_null = hist_df[~hist_df.category.isnull()]

        # Set the ordering
        if order_by == 'alphabetical':            
            hist_df_non_null = hist_df_non_null\
                .sort_values('category', ascending=ascending)
        else:
            if 'weights_{}'.format(order_by) not in hist_df_non_null.columns:
                raise Exception('Order by number exceeds number of DataFrames.')
            hist_df_non_null = hist_df_non_null\
                .sort_values('weights_{}'.format(order_by), ascending=ascending)

        if log:
            bin_height = [np.log10(hist_df_non_null['weights_{}'.format(i)]).tolist() for i in range(num_hists)]
        else:
            bin_height = [hist_df_non_null['weights_{}'.format(i)].tolist() for i in range(num_hists)]

        # If loc is '', then we do not want a NULL height
        # since we are ignoring NULL values
        if loc == '':
            null_height = [[]] * num_hists
        else:
            if log:
                null_height = [np.log10(hist_df_null['weights_{}'.format(i)]).tolist() for i in range(num_hists)]
            else:
                null_height = [hist_df_null['weights_{}'.format(i)].tolist() for i in range(num_hists)]

        return bin_height, null_height

    def _get_bin_width(num_hists):
        """Returns each bin width based on number of histograms."""
        if num_hists == 1:
            return 1
        else:
            return 0.8/num_hists

    def _plot_all_histograms(bin_left, bin_height, null_bin_left,
                             null_bin_height, bin_width):
        for i in range(num_hists):
            # If there are any null bins, plot them
            if len(null_bin_height[i]) > 0:
                plt.bar(null_bin_left[i], null_bin_height[i], bin_width,
                        hatch='x', color=color_palette[i])
            plt.bar(bin_left[i], bin_height[i], bin_width,
                    color=color_palette[i])

    def _plot_xticks(loc, bin_left, hist_df):
        """Plots the xtick labels."""
        # If there are any NULL categories
        if np.sum(hist_df.category.isnull()) > 0:
            if loc == 'left':
                xticks_loc = np.arange(num_categories) + 0.5
                plt.xticks(xticks_loc,
                           ['NULL'] + hist_df.dropna()['category'].tolist(),
                           rotation=90
                          )
            elif loc == 'right':
                xticks_loc = np.arange(num_categories) + 0.5
                plt.xticks(xticks_loc,
                           hist_df.dropna()['category'].tolist() + ['NULL'],
                           rotation=90
                          )
            elif loc == 'order':
                xticks_loc = np.arange(num_categories) + 0.5
                plt.xticks(xticks_loc,
                           hist_df['category'].fillna('NULL').tolist(),
                           rotation=90
                          )
            elif loc == '':
                xticks_loc = np.arange(num_categories) + 0.5
                plt.xticks(xticks_loc,
                           hist_df.dropna()['category'].tolist(),
                           rotation=90
                          )
        else:
            xticks_loc = np.arange(num_categories) + 0.5
            plt.xticks(xticks_loc,
                       hist_df.dropna()['category'].tolist(),
                       rotation=90
                      )

    def _plot_new_yticks(bin_height):
        """Changes yticks to log scale."""
        max_y_tick = int(np.ceil(np.max(bin_height))) + 1
        yticks = [10**i for i in range(max_y_tick)]
        yticks = ['1e{}'.format(i) for i in range(max_y_tick)]
        plt.yticks(range(max_y_tick), yticks)


    df_list, labels = _listify(df_list, labels)
    # Joins in all the df_list DataFrames so that we can pick a certain 
    # category and retrieve the count for each.
    hist_df = _join_freq_df(df_list)
    # Order them based on specified order
    hist_df = _get_bin_order(null_at, hist_df, order_by)

    num_hists = len(df_list)
    num_categories = _get_num_categories(hist_df)

    hist_df.set_index('category', inplace=True)

    # Normalize
    if normed:
        col_type = 'weights'
        hist_df = hist_df.filter(regex='weights_[0-9]+')
    else:
        col_type = 'freq'
        hist_df = hist_df.filter(regex='freq_[0-9]+')

    # Get ordering
    if order_by == 'alphabetical':
        if null_at == 'left':
            na_position='first'
        else:
            na_position='last'

        if null_at == '':
            hist_df = hist_df[~hist_df.index.isnull()]

        hist_df.sort_index(ascending=ascending,
                           na_position=na_position,
                           inplace=True
                          )

    elif isinstance(order_by, int):
        col_name = '{}_{}'.format(col_type, order_by)
        hist_df.sort_values(col_name, ascending=ascending, inplace=True)

    hist_df.plot(kind='bar', log=log)
        
    return hist_df


def plot_numeric_hists(df_list, labels=[], nbins=25, log=False, normed=False,
                       null_at='left',
                       color_palette=sns.color_palette('deep')):
    """Plots numerical histograms together.
    
    Parameters
    ----------
    df_list : A DataFrame or a list of DataFrames
        DataFrame or list of DataFrames which have two columns
        bin_loc and freq). Bin location marks the edges of the bins
        and the frequency is how many values fall in each bin.
    labels : str or list of str
        A string (for one histogram) or list of strings which sets the
        labels for the histograms
    nbins : int, default 25
        The desired number of bins
    log : bool, default False
        Whether to display y axis on log scale
    normed : bool, default False
        Whether to normalize histograms so that the heights of each bin
        sum up to 1. This is useful for plotting columns with different
        sizes
    null_at : str, default 'left'
        Which side to set a null value column. The options are:
            'left' - Put the null on the left side
            'right' - Put it on the right side
            '' - If left blank, leave out
    color_palette : list of tuples, default sns deep colour palette
        Seaborn colour palette, i.e., a list of tuples representing the
        colours.
    """
    
    def _check_for_nulls(df_list):
        """Returns a list of whether each list has a null column."""
        return [df.bin_loc.isnull().any() for df in df_list]

    def _get_null_weights(has_null, df_list):
        """If there are nulls, determine the weights.  Otherwise, set 
        weights to 0.
        
        Returns the list of null weights.
        """

        return [float(df[df.bin_loc.isnull()].weights)
                if is_null else 0 
                for is_null, df in zip(has_null, df_list)]

    def _get_data_type(bin_locs):
        """ Returns the data type in the histogram, i.e., whether it is
        numeric or a timetamp. This is important because it determines
        how we deal with the bins.
        """

        if 'float' in str(type(bin_locs[0][0])) or 'int' in str(type(bin_locs[0][0])):
            return 'numeric'
        elif str(type(bin_locs[0][0])) == "<class 'pandas.tslib.Timestamp'>":
            return 'timestamp'
        else:
            raise Exception('Bin data type not valid: {}'.format(type(bin_locs[0][0])))

    def _plot_hist(data_type, bin_locs, weights, labels, bins, log):
        """Plots the histogram for non-null values with corresponding
        labels if provided. This function will take also reduce the
        number of bins in the histogram. This is useful if we want to
        apply get_histogram_values for a large number of bins, then 
        experiment with plotting different bin amounts using the
        histogram values.
        """

        # If the bin type is numeric
        if data_type == 'numeric':
            if len(labels) > 0:
                _, bins, _ = plt.hist(x=bin_locs, weights=weights,
                                      label=labels, bins=nbins, log=log)
            else:
                _, bins, _ = plt.hist(x=bin_locs, weights=weights, bins=nbins,
                                      log=log)
            return bins

        # If the bin type is datetime or a timestamp
        elif data_type == 'timestamp':
            # Since pandas dataframes will convert timestamps and date
            # types to pandas.tslib.Timestamp types, we will need
            # to convert them to datetime since these can be plotted.
            datetime_list = [dt.to_pydatetime() for dt in bin_locs[0]]
            _, bins, _ = plt.hist(x=datetime_list, weights=weights[0],
                                  bins=nbins, log=log)
            return bins

    def _get_null_bin_width(data_type, bin_info, num_hists, null_weights):
        """Returns the width of each null bin."""
        bin_width = bin_info[1] - bin_info[0]
        if num_hists == 1:
            return bin_width
        else:
            return 0.8 * bin_width/len(null_weights)

    def _get_null_bin_left(data_type, loc, num_hists, bin_info, null_weights):
        """Gets the left index/indices or the null column(s)."""
        bin_width = bin_info[1] - bin_info[0]
        if loc == 'left':
            if num_hists == 1:
                return [bin_info[0] - bin_width]
            else:
                return [bin_info[0] - bin_width + bin_width*0.1 + i*_get_null_bin_width(data_type, bin_info, num_hists, null_weights) for i in range(num_hists)]
        elif loc == 'right':
            if num_hists == 1:
                return [bin_info[-1]]
            else:
                return [bin_width*0.1 + i*_get_null_bin_width(data_type, bin_info, num_hists, null_weights) + bin_info[-1] for i in range(num_hists)]
        elif loc == 'order':
            raise Exception('null_at = order is not supported for numeric histograms.')

    def _plot_null_xticks(loc, bins, xticks):
        """Given current xticks, plot appropriate NULL tick."""
        bin_width = bins[1] - bins[0]
        if loc == 'left':
            plt.xticks([bins[0] - bin_width*0.5] + xticks[1:].tolist(), ['NULL'] + [int(i) for i in xticks[1:]])
        elif loc == 'right':
            plt.xticks(xticks[:-1].tolist() + [bins[-1] + bin_width*0.5], [int(i) for i in xticks[:-1]] + ['NULL'])

    def _get_xlim(loc, has_null, bins, null_bin_left, null_bin_height):
        """Gets the x-limits for plotting."""
        if loc == '' or not np.any(has_null):
            # If we do not want to plot nulls, or if there are no nulls
            # in the data, then set the limits as the regular histogram
            # limits
            xlim_left = bins[0]
            xlim_right = bins[-1]
        else:
            xlim_left = min(bins.tolist() + null_bin_left)
            if loc == 'left':
                xlim_right = max(bins.tolist() + null_bin_left)
            elif loc == 'right':
                xlim_right = max(bins.tolist() + null_bin_left) + null_bin_height

        return xlim_left, xlim_right


    df_list, labels = _listify(df_list, labels)
    # Joins in all the df_list DataFrames
    # Number of histograms we want to overlay
    num_hists = len(df_list)

    # If any of the columns are null
    has_null = _check_for_nulls(df_list)
    _add_weights_column(df_list, normed)

    # Set color_palette
    sns.set_palette(color_palette)
    null_weights = _get_null_weights(has_null, df_list)
    
    df_list = [df.dropna() for df in df_list]
    weights = [df.weights for df in df_list]
    bin_locs = [df.bin_loc for df in df_list]
    
    data_type = _get_data_type(bin_locs)

    # Plot histograms and retrieve bins
    bin_info = _plot_hist(data_type, bin_locs, weights, labels, nbins, log)

    null_bin_width = _get_null_bin_width(data_type, bin_info, num_hists, null_weights)
    null_bin_left = _get_null_bin_left(data_type, null_at, num_hists, bin_info, null_weights)
    xticks, _ = plt.xticks()

    # If we are plotting NULLS and there are some, plot them and change xticks
    if null_at != '' and np.any(has_null):
        for i in range(num_hists):
            plt.bar(null_bin_left[i], null_weights[i], null_bin_width,
                    color=color_palette[i], hatch='x')
        if data_type == 'numeric':
            _plot_null_xticks(null_at, bin_info, xticks)
        elif data_type == 'timestamp':
            pass 
    # Set the x axis limits
    plt.xlim(_get_xlim(null_at, has_null, bin_info, null_bin_left, null_bin_width))


def plot_date_hists(df_list, labels=[], nbins=25, log=False, normed=False,
                    null_at='left',
                    color_palette=sns.color_palette('colorblind')):
    """Plots histograms by date.

    Inputs:
    df_list - A pandas DataFrame or a list of DataFrames which have two
              columns (bin_nbr and freq). The bin_nbr is the value of
              the histogram bin and freq is how many values fall in that
              bin.
    labels - A string (for one histogram) or list of strings which sets
             the labels for the histograms
    nbins - The desired number of bins (Default: 25)
    log - Boolean of whether to display y axis on log scale
          (Default: False)
    normed - Boolean of whether to normalize histograms so that the
             heights of each bin sum up to 1. This is useful for
             plotting columns with difference sizes (Default: False)
    null_at - Which side to set a null value column. Options are 'left'
              or 'right'. Leave it empty to not include (Default: left)
    color_palette - Seaborn colour palette, i.e., a list of tuples
                    representing the colours. (Default: sns deep color
                    palette)
    """

    df_list, labels = _listify(df_list, labels)
    # Joins in all the df_list DataFrames
    # Number of histograms we want to overlay
    num_hists = len(df_list)

    # If any of the columns are null
    has_null = _check_for_nulls(df_list)
    _add_weights_column(df_list, normed)

    # Set color_palette
    sns.set_palette(color_palette)
    null_weights = _get_null_weights(has_null, df_list)

    print has_null
    print null_weights


def plot_scatterplot(scatter_df, s=20, c=sns.color_palette('deep')[0],
                     plot_type='scatter', by_size=True, by_opacity=True,
                     marker='o', cmap='Blues'):
    """Plots a scatter plot based on the computed scatter plot bins.

    Parameters
    ----------
    scatter_df : DataFrame
        DataFrame with three columns: scat_bin_x, scat_bin_y, and freq.
        The columns scat_bin_x and scat_bin_y are the bins along the 
        x and y axes and freq is how many values fall into the bin.
    s : int, default 20
        The size of each point
    c : tuple or string, default seaborn deep blue
        The colour of the plot
    by_size : boolean, default True
        If True, then the size of each plotted point will be
        proportional to its frequency. Otherwise, each point will be a
        constant size specified by s.
    by_opacity : boolean, default True
        If True, then the opacity of each plotted point will be
        proportional to its frequency. A darker bin immplies more data
        in that bin.
    marker : str, default 'o'
        matplotlib marker
    """

    if plot_type not in ['scatter', 'heatmap']:
        raise ValueError("plot_type must be either 'scatter' or 'heatmap'.")

    if plot_type == 'scatter':
        if not by_size and not by_opacity:
            raise Exception('Scatterplot must be plotted by size and/or opacity.')

        if by_size:
            plot_size = 20*scatter_df.freq
        else:
            plot_size = 20

        if by_opacity:
            colour = np.zeros((scatter_df.shape[0], 4))
            colour[:, :3] = c
            # Add alpha component
            colour[:, 3] = scatter_df.freq/scatter_df.freq.max()
            lw = 0 
        else:
            colour = c
            lw = 0.5

        plt.scatter(scatter_df.scat_bin_x, scatter_df.scat_bin_y,
                    c=colour, s=plot_size, lw=lw, marker=marker)

    elif plot_type == 'heatmap':
        num_x = len(scatter_df.scat_bin_x.value_counts())
        num_y = len(scatter_df.scat_bin_y.value_counts())

        x = scatter_df['scat_bin_x'].values.reshape(num_x, num_y)
        y = scatter_df['scat_bin_y'].values.reshape(num_x, num_y)
        z = scatter_df['freq'].values.reshape(num_x, num_y) 

        plt.pcolor(x, y, z, cmap=cmap)
        plt.xlim(x.min(), x.max())
        plt.ylim(y.min(), y.max())
