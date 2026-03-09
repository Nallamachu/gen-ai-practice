# Streamlit Basics
# Use the below command to run the streamlit app:
# python -m streamlit run app.py

# pip install streamlit

import streamlit as st
import pandas as pd
import numpy as np

st.title("Streamlit Basics")
st.header("This is a header")
st.subheader("This is a subheader")
st.markdown("This is a markdown")
st.write("Hello World")
st.write("This is a text")
st.write(pd.DataFrame(np.random.randn(10, 5)))
st.line_chart(np.random.randn(10, 5))
st.bar_chart(np.random.randn(10, 5))